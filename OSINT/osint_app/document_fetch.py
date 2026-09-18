"""SSRF-safe fetch for public documents (PDF/HTML/TXT) -- see SECURITY /
PIVOT SAFETY CONTROLS in the spec. Only http/https, hostname DNS-resolved
and IP-range checked before connecting AND before following every redirect
hop, response size capped via streaming (not trusted from Content-Length
alone), request time capped.

Known residual risk: DNS rebinding -- an attacker's DNS server could answer
our pre-connect resolution with a public IP, then answer the OS resolver's
connect-time lookup a moment later with a private one, since httpx does its
own resolution when it actually opens the socket. Closing that fully needs
pinning the resolved socket at the transport layer, which isn't exposed
cleanly through httpx's public API. What's implemented here blocks the
overwhelmingly common case (a URL that is simply, statically, an internal
address, or a redirect chain that walks into one) which is the realistic
threat for a service that fetches already-discovered public URLs -- not
raw, unvalidated attacker-supplied targets.
"""
import ipaddress
import socket
from dataclasses import dataclass

import httpx

from osint_app.config import settings

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; OSINT-Phase1/1.0; +document-extraction)"


class DocumentUnavailable(Exception):
    pass


def _is_public_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def validate_url(url: str) -> None:
    """Raises DocumentUnavailable if `url` is not a safe fetch target."""
    parsed = httpx.URL(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise DocumentUnavailable(f"scheme not allowed for document fetch: {parsed.scheme!r}")
    hostname = parsed.host
    if not hostname:
        raise DocumentUnavailable("URL has no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise DocumentUnavailable(f"DNS resolution failed for {hostname}: {exc}") from exc
    if not infos:
        raise DocumentUnavailable(f"DNS resolution returned no addresses for {hostname}")

    for info in infos:
        ip_str = info[4][0]
        if not _is_public_ip(ip_str):
            raise DocumentUnavailable(f"{hostname} resolves to a non-public address ({ip_str}) -- blocked")


@dataclass(frozen=True)
class FetchedDocument:
    url: str  # final URL after redirects
    content_type: str
    raw_bytes: bytes


async def fetch_document(url: str) -> FetchedDocument:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(current_url)

        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=settings.document_fetch_timeout_seconds
            ) as client:
                async with client.stream("GET", current_url, headers={"User-Agent": USER_AGENT}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise DocumentUnavailable(f"redirect with no Location header from {current_url}")
                        current_url = str(httpx.URL(current_url).join(location))
                        continue

                    if resp.status_code != 200:
                        raise DocumentUnavailable(f"HTTP {resp.status_code} fetching {current_url}")

                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > settings.max_document_bytes:
                        raise DocumentUnavailable(
                            f"document too large ({content_length} bytes > {settings.max_document_bytes} limit)"
                        )

                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > settings.max_document_bytes:
                            raise DocumentUnavailable(
                                f"document exceeded {settings.max_document_bytes} byte limit while streaming"
                            )
                        chunks.append(chunk)

                    return FetchedDocument(url=current_url, content_type=content_type, raw_bytes=b"".join(chunks))
        except httpx.HTTPError as exc:
            raise DocumentUnavailable(f"request failed for {current_url}: {exc}") from exc

    raise DocumentUnavailable(f"too many redirects (> {MAX_REDIRECTS}) starting from {url}")
