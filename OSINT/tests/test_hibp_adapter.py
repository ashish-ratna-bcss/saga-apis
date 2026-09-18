import hashlib

import httpx
import pytest

from osint_app.adapters.hibp_adapter import HibpUnavailable, check_pwned_password


@pytest.mark.asyncio
async def test_pwned_password_detected(monkeypatch):
    password = "password123"
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()
    suffix = sha1[5:]

    async def fake_get(self, url, headers=None):
        return httpx.Response(
            200,
            text=f"{suffix}:2266543\nAAAA0000000000000000000000000000000:1",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await check_pwned_password(password)
    assert result.pwned is True
    assert result.times_seen == 2266543


@pytest.mark.asyncio
async def test_password_not_found_in_range(monkeypatch):
    async def fake_get(self, url, headers=None):
        return httpx.Response(
            200, text="AAAA0000000000000000000000000000000:1", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await check_pwned_password("some-unrelated-password")
    assert result.pwned is False
    assert result.times_seen == 0


@pytest.mark.asyncio
async def test_empty_password_raises():
    with pytest.raises(ValueError):
        await check_pwned_password("")


@pytest.mark.asyncio
async def test_http_error_raises_unavailable(monkeypatch):
    async def fake_get(self, url, headers=None):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(HibpUnavailable):
        await check_pwned_password("whatever")


@pytest.mark.asyncio
async def test_never_sends_full_hash_only_prefix(monkeypatch):
    captured_urls = []

    async def fake_get(self, url, headers=None):
        captured_urls.append(url)
        return httpx.Response(200, text="", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    password = "correct horse battery staple"
    full_sha1 = hashlib.sha1(password.encode()).hexdigest().upper()
    await check_pwned_password(password)

    assert len(captured_urls) == 1
    assert full_sha1 not in captured_urls[0]
    assert full_sha1[:5] in captured_urls[0]
