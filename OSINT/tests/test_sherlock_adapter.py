import pytest

from osint_app.adapters import sherlock_adapter
from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.enums import ClaimType


class _FakeStatus:
    def __init__(self, status):
        self.status = status


@pytest.mark.asyncio
async def test_run_maps_only_claimed_sites(monkeypatch):
    from sherlock_project.result import QueryStatus

    fake_raw = {
        "GitHub": {
            "url_main": "https://github.com",
            "url_user": "https://github.com/rahul_123",
            "status": _FakeStatus(QueryStatus.CLAIMED),
            "http_status": 200,
        },
        "SomeSite": {
            "url_main": "https://example.com",
            "url_user": "https://example.com/rahul_123",
            "status": _FakeStatus(QueryStatus.AVAILABLE),
            "http_status": 404,
        },
        "Broken": {
            "url_main": "https://broken.example",
            "url_user": None,
            "status": _FakeStatus(QueryStatus.UNKNOWN),
            "http_status": None,
        },
    }

    async def fake_to_thread(func, *args):
        return fake_raw

    monkeypatch.setattr(sherlock_adapter.asyncio, "to_thread", fake_to_thread)

    adapter = sherlock_adapter.SherlockAdapter()
    results = await adapter.run("rahul_123")

    assert len(results) == 1
    result = results[0]
    assert result.value == "https://github.com/rahul_123"
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION
    assert result.evidence.metadata["site"] == "GitHub"


@pytest.mark.asyncio
async def test_unavailable_when_not_installed(monkeypatch):
    adapter = sherlock_adapter.SherlockAdapter()

    async def fake_unavailable():
        return False

    monkeypatch.setattr(adapter, "is_available", fake_unavailable)

    with pytest.raises(SourceUnavailable):
        await adapter.run("rahul_123")


@pytest.mark.asyncio
async def test_overall_timeout_raises_source_unavailable(monkeypatch):

    async def fake_to_thread(func, *args):
        raise TimeoutError()

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError()

    monkeypatch.setattr(sherlock_adapter.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(sherlock_adapter.asyncio, "wait_for", fake_wait_for)

    adapter = sherlock_adapter.SherlockAdapter()
    with pytest.raises(SourceUnavailable):
        await adapter.run("rahul_123")
