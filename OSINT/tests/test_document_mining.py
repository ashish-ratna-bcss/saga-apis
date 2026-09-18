import pytest

from osint_app.config import settings
from osint_app.document_mining import select_document_urls
from osint_app.entity_resolution import get_or_create_root_entity
from osint_app.enums import IdentifierType
from osint_app.models import Evidence, Investigation, SearchJob


def _make_investigation(db_session) -> Investigation:
    inv = Investigation(input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123")
    db_session.add(inv)
    db_session.flush()
    return inv


def _url_evidence(inv, entity, url: str) -> Evidence:
    return Evidence(
        investigation_id=inv.id, entity_id=entity.id, source_name="public_web", source_type="public_association",
        query="q", raw_value=url, normalized_value=url, entity_type="URL", source_url=url,
        confidence=0.4, extraction_method="searxng_search", raw_metadata={},
    )


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "document_mining_enabled", True)
    monkeypatch.setattr(settings, "max_documents_per_investigation", 10)


def test_selects_url_evidence_only(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev1 = _url_evidence(inv, root, "https://a.example.com/doc.pdf")
    ev2 = Evidence(
        investigation_id=inv.id, entity_id=root.id, source_name="sherlock", source_type="public_association",
        query="q", raw_value="rahul_123", normalized_value="rahul_123", entity_type="SocialProfile",
        confidence=0.55, extraction_method="sherlock_module", raw_metadata={},
    )
    db_session.add_all([ev1, ev2])
    db_session.flush()

    urls = select_document_urls(db_session, inv, [ev1, ev2], "document_extraction")
    assert urls == ["https://a.example.com/doc.pdf"]


def test_dedupes_repeated_urls(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev1 = _url_evidence(inv, root, "https://a.example.com/x")
    ev2 = _url_evidence(inv, root, "https://a.example.com/x")
    db_session.add_all([ev1, ev2])
    db_session.flush()

    urls = select_document_urls(db_session, inv, [ev1, ev2], "document_extraction")
    assert urls == ["https://a.example.com/x"]


def test_respects_remaining_budget(db_session, monkeypatch):
    monkeypatch.setattr(settings, "max_documents_per_investigation", 2)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    evs = [_url_evidence(inv, root, f"https://a.example.com/{i}") for i in range(5)]
    db_session.add_all(evs)
    db_session.flush()

    urls = select_document_urls(db_session, inv, evs, "document_extraction")
    assert len(urls) == 2


def test_already_mined_count_reduces_budget(db_session, monkeypatch):
    monkeypatch.setattr(settings, "max_documents_per_investigation", 2)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    db_session.add(SearchJob(investigation_id=inv.id, source_name="document_extraction", status="completed"))
    db_session.add(SearchJob(investigation_id=inv.id, source_name="document_extraction", status="completed"))
    db_session.flush()

    evs = [_url_evidence(inv, root, "https://a.example.com/new")]
    db_session.add_all(evs)
    db_session.flush()

    urls = select_document_urls(db_session, inv, evs, "document_extraction")
    assert urls == []  # budget of 2 already fully consumed by prior jobs


def test_disabled_returns_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "document_mining_enabled", False)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _url_evidence(inv, root, "https://a.example.com/x")
    db_session.add(ev)
    db_session.flush()

    assert select_document_urls(db_session, inv, [ev], "document_extraction") == []


def test_empty_or_missing_raw_value_ignored(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _url_evidence(inv, root, "")
    db_session.add(ev)
    db_session.flush()

    assert select_document_urls(db_session, inv, [ev], "document_extraction") == []
