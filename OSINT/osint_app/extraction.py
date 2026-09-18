"""Candidate-identifier extraction from free text (search snippets, titles,
URLs). Pure functions -- no DB access, no adapter calls, so the pivot engine
can be tested independently of the network.

Different candidate types carry different base confidence -- a structured
match (email regex, a phonenumbers-*validated* phone number) is a much
stronger signal than a bare word that might be a name (see spec's
"IMPORTANT: EXTRACTION" -- "abc@example.com is a strong EMAIL candidate" vs
"Rahul is a weak PERSON_NAME candidate").

PERSON_NAME and COMPANY extraction from free text is deliberately NOT
implemented: reliably telling "Rahul Kumar" (a name) apart from "Reset
Password" or "Contact Us" in arbitrary scraped text needs real NER, not
regex. A naive version would produce exactly the over-generated, low-value
candidates the spec warns against -- documented limitation (see README),
not a silent gap.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import phonenumbers

from osint_app.enums import IdentifierType

MAX_SCAN_CHARS = 5000  # search snippets/titles are always short; bounds worst-case regex/matcher cost

EMAIL_CONFIDENCE = 0.75
PHONE_CONFIDENCE = 0.80
USERNAME_CONFIDENCE = 0.60
DOMAIN_BASE_CONFIDENCE = 0.35
DOMAIN_CORROBORATED_CONFIDENCE = 0.60

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}\b")

# Known social-platform profile URL patterns -> the username is a path
# segment. Kept small and explicit, not a generic "last path segment"
# heuristic -- that would turn /login, /about, /search on arbitrary sites
# into fake "usernames".
_PROFILE_URL_PATTERNS = [
    re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_-]{1,39})/?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?twitter\.com/([A-Za-z0-9_]{1,15})/?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})/?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?reddit\.com/u(?:ser)?/([A-Za-z0-9_-]{3,20})/?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?t\.me/([A-Za-z0-9_]{5,32})/?$", re.IGNORECASE),
]
_PROFILE_URL_RESERVED_SEGMENTS = {
    "login", "signup", "sign-up", "about", "search", "settings", "home",
    "explore", "help", "contact", "download", "join", "signin", "sign-in",
    "terms", "privacy", "hashtag", "share", "intent",
}

# Domains that are near-universally present in search results regardless of
# the actual investigation subject (social platforms, marketplaces, generic
# infra) -- pivoting into "investigate whatsapp.com as a company" for every
# single phone search is pure noise, not a discovery.
GENERIC_DOMAIN_BLOCKLIST = {
    "whatsapp.com", "web.whatsapp.com", "faq.whatsapp.com",
    "truecaller.com", "github.com", "gitlab.com", "twitter.com", "x.com",
    "instagram.com", "facebook.com", "linkedin.com", "reddit.com", "t.me",
    "youtube.com", "wikipedia.org", "google.com", "play.google.com",
    "apps.apple.com", "amazon.com", "amazon.in", "flipkart.com",
    "walmart.com", "target.com", "ebay.com", "microsoft.com",
    "support.microsoft.com", "support.apple.com", "yelp.com",
}


@dataclass(frozen=True)
class Candidate:
    identifier_type: IdentifierType
    value: str
    confidence: float
    extraction_method: str


def extract_emails(text: str) -> list[Candidate]:
    return [
        Candidate(IdentifierType.EMAIL, m.group(0), EMAIL_CONFIDENCE, "regex_email_extraction")
        for m in _EMAIL_RE.finditer(text[:MAX_SCAN_CHARS])
    ]


def extract_phones(text: str, default_region: str = "IN") -> list[Candidate]:
    candidates: list[Candidate] = []
    try:
        for match in phonenumbers.PhoneNumberMatcher(text[:MAX_SCAN_CHARS], default_region):
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            candidates.append(Candidate(IdentifierType.PHONE, e164, PHONE_CONFIDENCE, "phonenumbers_matcher"))
    except Exception:
        pass  # matcher can raise on pathological input; extraction must never crash a job
    return candidates


def extract_usernames_from_url(url: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    stripped = url.strip()[:MAX_SCAN_CHARS]
    for pattern in _PROFILE_URL_PATTERNS:
        m = pattern.match(stripped)
        if m:
            username = m.group(1)
            if username.lower() not in _PROFILE_URL_RESERVED_SEGMENTS:
                candidates.append(
                    Candidate(IdentifierType.USERNAME, username, USERNAME_CONFIDENCE, "profile_url_pattern")
                )
    return candidates


def extract_domain_from_url(url: str) -> str | None:
    try:
        netloc = urlparse(url.strip()).netloc.lower()
    except ValueError:
        return None
    if not netloc:
        return None
    host = netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def is_generic_domain(domain: str) -> bool:
    return domain.lower() in GENERIC_DOMAIN_BLOCKLIST


def extract_all(text: str, url: str | None = None) -> list[Candidate]:
    """Runs the text-based extractors over `text` (title+snippet, typically)
    plus the URL-shaped extractor over `url` if given. Domain candidates are
    NOT produced here -- the pivot engine derives those separately since a
    domain needs cross-evidence corroboration (see DOMAIN_BASE_CONFIDENCE),
    not just a single mention."""
    candidates = list(extract_emails(text))
    candidates.extend(extract_phones(text))
    if url:
        candidates.extend(extract_usernames_from_url(url))
    return candidates
