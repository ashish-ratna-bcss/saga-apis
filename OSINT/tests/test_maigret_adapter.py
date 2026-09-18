import pytest

from osint_app.adapters import maigret_adapter
from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.enums import ClaimType


class _FakeStatus:
    def __init__(self, found: bool):
        self._found = found

    def is_found(self):
        return self._found


@pytest.mark.asyncio
async def test_run_maps_only_claimed_sites(monkeypatch):
    fake_raw = {
        "GitHub": {"url_main": "https://github.com", "url_user": "https://github.com/rahul_123",
                    "status": _FakeStatus(True), "http_status": 200},
        "SomeSite": {"url_main": "https://example.com", "url_user": "https://example.com/rahul_123",
                      "status": _FakeStatus(False), "http_status": 404},
    }

    class _FakeDb:
        def ranked_sites_dict(self, top):
            return {}

    async def fake_to_thread(func, *args):
        return _FakeDb()

    async def fake_run_maigret(**kwargs):
        return fake_raw

    monkeypatch.setattr(maigret_adapter.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr("maigret.checking.maigret", fake_run_maigret)

    adapter = maigret_adapter.MaigretAdapter()
    results = await adapter.run("rahul_123")

    assert len(results) == 1
    result = results[0]
    assert result.value == "https://github.com/rahul_123"
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION
    assert result.evidence.metadata["site"] == "GitHub"


@pytest.mark.asyncio
async def test_unavailable_when_not_installed(monkeypatch):
    adapter = maigret_adapter.MaigretAdapter()

    async def fake_unavailable():
        return False

    monkeypatch.setattr(adapter, "is_available", fake_unavailable)

    with pytest.raises(SourceUnavailable):
        await adapter.run("rahul_123")
