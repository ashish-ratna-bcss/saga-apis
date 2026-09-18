"""Identifier detection + normalization.

Every identifier is normalized before it touches the database or an adapter,
so repeated searches for the same underlying identifier fingerprint to the
same investigation/entity instead of creating duplicates (see IDEMPOTENCY).
"""
import re
from urllib.parse import urlparse

import phonenumbers
from phonenumbers import NumberParseException

from osint_app.enums import IdentifierType

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$")


class NormalizationError(ValueError):
    pass


def normalize_phone(raw: str, default_region: str = "IN") -> str:
    raw = raw.strip()
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException as exc:
        raise NormalizationError(f"invalid phone number: {exc}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise NormalizationError("phone number failed validity check")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str) -> str:
    raw = raw.strip()
    if not _EMAIL_RE.match(raw):
        raise NormalizationError("invalid email address")
    local, _, domain = raw.rpartition("@")
    return f"{local}@{domain}".lower()


def normalize_username(raw: str) -> str:
    raw = raw.strip().lstrip("@")
    if not raw or re.search(r"\s", raw):
        raise NormalizationError("invalid username")
    return raw.lower()


def normalize_domain(raw: str) -> str:
    raw = raw.strip()
    if "//" in raw or raw.startswith("http"):
        raw = urlparse(raw).netloc or raw
    raw = raw.lower().split("/")[0].split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    if not _DOMAIN_RE.match(raw):
        raise NormalizationError("invalid domain")
    return raw


def normalize_person_name(raw: str) -> str:
    raw = " ".join(raw.strip().split())
    if not raw:
        raise NormalizationError("empty person name")
    return raw.title()


def detect_identifier_type(raw: str) -> IdentifierType:
    """Best-effort heuristic detection when the caller doesn't specify a type."""
    candidate = raw.strip()
    if _EMAIL_RE.match(candidate):
        return IdentifierType.EMAIL
    digits_and_symbols = re.sub(r"[\s()+-]", "", candidate)
    if digits_and_symbols.isdigit() and len(digits_and_symbols) >= 7:
        return IdentifierType.PHONE
    lowered = candidate.lower()
    bare_domain = lowered[4:] if lowered.startswith("www.") else lowered
    if candidate.startswith("http") or _DOMAIN_RE.match(bare_domain):
        return IdentifierType.DOMAIN
    if candidate.startswith("@") or (" " not in candidate and re.match(r"^[A-Za-z0-9_.]+$", candidate)):
        return IdentifierType.USERNAME
    return IdentifierType.PERSON_NAME


_NORMALIZERS = {
    IdentifierType.PHONE: normalize_phone,
    IdentifierType.EMAIL: normalize_email,
    IdentifierType.USERNAME: normalize_username,
    IdentifierType.DOMAIN: normalize_domain,
    IdentifierType.PERSON_NAME: normalize_person_name,
}


def normalize_identifier(raw: str, identifier_type: IdentifierType | None = None) -> tuple[IdentifierType, str]:
    if not raw or not raw.strip():
        raise NormalizationError("identifier is empty")
    resolved_type = identifier_type or detect_identifier_type(raw)
    normalized = _NORMALIZERS[resolved_type](raw)
    return resolved_type, normalized
