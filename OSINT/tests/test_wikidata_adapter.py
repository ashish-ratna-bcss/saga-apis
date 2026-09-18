import httpx
import pytest

from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.adapters.wikidata_adapter import (
    SAME_NAME_MATCH_CONFIDENCE,
    STRUCTURED_FACT_CONFIDENCE,
    WikidataAdapter,
)
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _search_response(items: list[dict]) -> dict:
    return {"search": items}


def _entity_response(qid: str, *, claims: dict, label: str) -> dict:
    return {"entities": {qid: {"labels": {"en": {"value": label}}, "claims": claims}}}


def _string_claim(value: str) -> list[dict]:
    return [{"mainsnak": {"datavalue": {"value": value}}}]


def _item_claim(qid: str) -> list[dict]:
    return [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": qid, "numeric-id": int(qid[1:])}}}}]


def _mock_client(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)
    return factory


@pytest.mark.asyncio
async def test_no_exact_match_returns_nothing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Different Name", "description": "x"}]))

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Rahul Kumar")
    assert results == []


@pytest.mark.asyncio
async def test_exact_match_is_case_insensitive(monkeypatch):
    calls = {"search": 0, "entity": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "wbsearchentities" in str(request.url):
            calls["search"] += 1
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "rahul kumar", "description": "x"}]))
        calls["entity"] += 1
        return httpx.Response(200, json=_entity_response("Q1", claims={}, label="rahul kumar"))

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Rahul Kumar")
    assert len(results) == 1
    assert calls["entity"] == 1


@pytest.mark.asyncio
async def test_human_match_produces_person_entity_with_capped_confidence(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "wbsearchentities" in str(request.url):
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Test Person", "description": "a human"}]))
        return httpx.Response(
            200,
            json=_entity_response("Q1", claims={"P31": _item_claim("Q5")}, label="Test Person"),
        )

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Test Person")
    assert len(results) == 1
    result = results[0]
    assert result.entity_type == EntityType.PERSON
    assert result.confidence == SAME_NAME_MATCH_CONFIDENCE
    assert result.confidence < settings.pivot_confidence_threshold  # deliberately non-cascading, see module docstring
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION
    assert result.status == AdapterStatus.FOUND


@pytest.mark.asyncio
async def test_non_human_match_produces_company_entity(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "wbsearchentities" in str(request.url):
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Test Corp", "description": "a company"}]))
        return httpx.Response(200, json=_entity_response("Q1", claims={"P31": _item_claim("Q4830453")}, label="Test Corp"))

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Test Corp")
    assert results[0].entity_type == EntityType.COMPANY


@pytest.mark.asyncio
async def test_website_claim_produces_url_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "wbsearchentities" in str(request.url):
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Test Corp", "description": "d"}]))
        return httpx.Response(
            200,
            json=_entity_response(
                "Q1", claims={"P31": _item_claim("Q4830453"), "P856": _string_claim("https://testcorp.example/")},
                label="Test Corp",
            ),
        )

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Test Corp")
    url_results = [r for r in results if r.entity_type == EntityType.URL]
    assert len(url_results) == 1
    assert url_results[0].value == "https://testcorp.example/"
    assert url_results[0].confidence == STRUCTURED_FACT_CONFIDENCE


@pytest.mark.asyncio
async def test_social_handle_claims_produce_social_profile_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "wbsearchentities" in str(request.url):
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Test Person", "description": "d"}]))
        return httpx.Response(
            200,
            json=_entity_response(
                "Q1",
                claims={"P31": _item_claim("Q5"), "P2002": _string_claim("testhandle")},
                label="Test Person",
            ),
        )

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Test Person")
    profiles = [r for r in results if r.entity_type == EntityType.SOCIAL_PROFILE]
    assert len(profiles) == 1
    assert profiles[0].value == "https://twitter.com/testhandle"


@pytest.mark.asyncio
async def test_employer_claim_resolves_label_and_produces_company_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "wbsearchentities" in url:
            return httpx.Response(200, json=_search_response([{"id": "Q1", "label": "Test Person", "description": "d"}]))
        if "Q1.json" in url:
            return httpx.Response(
                200,
                json=_entity_response("Q1", claims={"P31": _item_claim("Q5"), "P108": _item_claim("Q999")}, label="Test Person"),
            )
        return httpx.Response(200, json=_entity_response("Q999", claims={}, label="Employer Co"))

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Test Person")
    companies = [r for r in results if r.entity_type == EntityType.COMPANY]
    assert len(companies) == 1
    assert companies[0].value == "Employer Co"


@pytest.mark.asyncio
async def test_disambiguation_returns_multiple_candidates_not_a_silent_pick(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "wbsearchentities" in url:
            return httpx.Response(
                200,
                json=_search_response(
                    [
                        {"id": "Q1", "label": "Same Name", "description": "first notable person"},
                        {"id": "Q2", "label": "Same Name", "description": "second, unrelated notable person"},
                    ]
                ),
            )
        qid = "Q1" if "Q1.json" in url else "Q2"
        return httpx.Response(200, json=_entity_response(qid, claims={"P31": _item_claim("Q5")}, label="Same Name"))

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    results = await WikidataAdapter().run("Same Name")
    person_results = [r for r in results if r.entity_type == EntityType.PERSON]
    assert len(person_results) == 2
    assert {r.value for r in person_results} == {"Same Name (Q1)", "Same Name (Q2)"}


@pytest.mark.asyncio
async def test_search_failure_raises_source_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr("osint_app.adapters.wikidata_adapter.httpx.AsyncClient", _mock_client(handler))

    with pytest.raises(SourceUnavailable):
        await WikidataAdapter().run("Anyone")


@pytest.mark.asyncio
async def test_is_available_always_true():
    assert await WikidataAdapter().is_available() is True


# --- live network test: a real, stable, notable public figure

@pytest.mark.asyncio
async def test_live_lookup_real_notable_person():
    """Live-verified against real Wikidata, not mocked."""
    results = await WikidataAdapter().run("Sundar Pichai")
    assert len(results) >= 1
    person_results = [r for r in results if r.entity_type == EntityType.PERSON]
    assert len(person_results) == 1
    assert person_results[0].confidence == SAME_NAME_MATCH_CONFIDENCE
    # a globally notable tech CEO should have at least one resolved employer
    companies = [r for r in results if r.entity_type == EntityType.COMPANY]
    assert len(companies) >= 1
