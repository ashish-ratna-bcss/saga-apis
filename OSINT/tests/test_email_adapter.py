import pytest

from osint_app.adapters import email_adapter
from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.enums import ClaimType, EntityType


@pytest.mark.asyncio
async def test_run_maps_only_existing_accounts(monkeypatch):
    fake_raw = [
        {"name": "github", "domain": "github.com", "method": "register", "rateLimit": False, "exists": True},
        {"name": "adobe", "domain": "adobe.com", "method": "register", "rateLimit": False, "exists": False},
        {"name": "twitter", "domain": "twitter.com", "method": "register", "rateLimit": True, "exists": None},
    ]

    async def fake_to_thread(func, *args):
        return fake_raw

    monkeypatch.setattr(email_adapter.asyncio, "to_thread", fake_to_thread)

    adapter = email_adapter.HoleheAdapter()
    results = await adapter.run("test@example.com")

    assert len(results) == 1
    result = results[0]
    assert result.value == "github.com"
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION
    assert result.entity_type == EntityType.SOCIAL_PROFILE
    assert result.evidence.url == "https://github.com"


@pytest.mark.asyncio
async def test_unavailable_when_dependency_missing(monkeypatch):
    adapter = email_adapter.HoleheAdapter()
    monkeypatch.setattr(adapter, "is_available", lambda: _false())

    with pytest.raises(SourceUnavailable):
        await adapter.run("test@example.com")


async def _false():
    return False


@pytest.mark.asyncio
async def test_execution_error_raises_source_unavailable(monkeypatch):
    async def fake_to_thread(func, *args):
        raise RuntimeError("network blocked")

    monkeypatch.setattr(email_adapter.asyncio, "to_thread", fake_to_thread)

    adapter = email_adapter.HoleheAdapter()
    with pytest.raises(SourceUnavailable):
        await adapter.run("test@example.com")
