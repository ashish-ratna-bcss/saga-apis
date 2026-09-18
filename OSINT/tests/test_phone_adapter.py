import pytest

from osint_app.adapters.base import AdapterStatus
from osint_app.adapters.phone_adapter import PhoneAdapter, generate_search_queries
from osint_app.enums import ClaimType
from osint_app.normalization import normalize_phone


@pytest.mark.asyncio
async def test_phone_adapter_returns_technical_claim():
    number = normalize_phone("+91 98765 43210")
    adapter = PhoneAdapter()
    assert await adapter.is_available() is True

    results = await adapter.run(number)
    assert len(results) == 1
    result = results[0]
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.TECHNICAL
    assert result.evidence.metadata["region_code"] == "IN"
    assert result.evidence.metadata["line_type"] == "MOBILE"
    assert result.evidence.metadata["is_valid"] is True


def test_generate_search_queries_includes_e164_and_national():
    queries = generate_search_queries("+919876543210")
    assert any("+919876543210" in q for q in queries)
    assert any("9876543210" in q and "+" not in q for q in queries)
