"""HIBP Pwned Passwords (k-Anonymity API) -- https://haveibeenpwned.com/API/v3#PwnedPasswords

This is a PASSWORD exposure check, not an email-breach lookup. HIBP's actual
email-breach API requires a paid subscription (excluded from Phase 1); Pwned
Passwords is the one HIBP endpoint that is free, keyless and safe to call
live because of its k-anonymity design: only the first 5 hex chars of the
password's SHA-1 hash are ever sent over the network, never the password or
full hash. Deliberately NOT wired into the identifier pipeline -- password is
not one of the five supported identifier types (PHONE/EMAIL/USERNAME/
PERSON_NAME/DOMAIN) -- exposed instead as a standalone investigator utility.
"""
import hashlib
from dataclasses import dataclass

import httpx

PWNED_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"


class HibpUnavailable(Exception):
    pass


@dataclass
class PwnedPasswordResult:
    pwned: bool
    times_seen: int
    sha1_prefix: str  # only the 5-char prefix that was ever transmitted


async def check_pwned_password(password: str, timeout_seconds: int = 10) -> PwnedPasswordResult:
    if not password:
        raise ValueError("password must not be empty")

    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 (HIBP protocol requires SHA-1)
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(
                PWNED_RANGE_URL.format(prefix=prefix), headers={"Add-Padding": "true"}
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HibpUnavailable(f"HIBP Pwned Passwords request failed: {exc}") from exc

    times_seen = 0
    for line in resp.text.splitlines():
        candidate_suffix, _, count = line.partition(":")
        if candidate_suffix.strip() == suffix:
            times_seen = int(count.strip() or 0)
            break

    return PwnedPasswordResult(pwned=times_seen > 0, times_seen=times_seen, sha1_prefix=prefix)
