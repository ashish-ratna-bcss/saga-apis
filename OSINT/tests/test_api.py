import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from osint_app import db as db_module
from osint_app import orchestrator


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test_api.db"
    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    test_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", test_session_local)
    monkeypatch.setattr(orchestrator, "SessionLocal", test_session_local)

    from osint_app.config import settings

    monkeypatch.setattr(settings, "searxng_url", None)  # deterministic: public_web -> unavailable

    from osint_app.adapters.wikidata_adapter import WikidataAdapter

    async def _wikidata_unavailable(self):
        return False

    # deterministic + offline: real Wikidata content can change over time
    # (see test_wikidata_adapter.py for live-network-backed behavior tests)
    monkeypatch.setattr(WikidataAdapter, "is_available", _wikidata_unavailable)

    from osint_app.main import app

    with TestClient(app) as c:
        yield c


def _wait_for_terminal_status(client: TestClient, investigation_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    terminal = {"completed", "failed", "partial", "unavailable", "cancelled"}
    while time.time() < deadline:
        resp = client.get(f"/api/v1/investigations/{investigation_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in terminal:
            return body
        time.sleep(0.1)
    raise AssertionError("investigation did not reach a terminal status in time")


def test_create_investigation_normalizes_and_returns_queued(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Rahul Kumar"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["identifier_type"] == "PERSON_NAME"
    assert body["normalized_identifier"] == "Rahul Kumar"
    assert body["status"] in {"queued", "running", "completed", "unavailable"}


def test_invalid_identifier_returns_422(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "not-a-phone", "identifier_type": "PHONE"})
    assert resp.status_code == 422


def test_idempotency_key_replays_same_investigation(client: TestClient):
    headers = {"Idempotency-Key": "test-key-1"}
    body = {"input_identifier": "Idempotent Person"}
    first = client.post("/api/v1/investigations", json=body, headers=headers)
    second = client.post("/api/v1/investigations", json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_idempotency_key_conflict_on_different_body(client: TestClient):
    headers = {"Idempotency-Key": "test-key-2"}
    client.post("/api/v1/investigations", json={"input_identifier": "Person One"}, headers=headers)
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Person Two"}, headers=headers)
    assert resp.status_code == 409


def test_full_pipeline_runs_and_marks_public_web_unavailable(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    investigation_id = resp.json()["id"]

    detail = _wait_for_terminal_status(client, investigation_id)
    # both PERSON_NAME adapters (wikidata, public_web) are unavailable in this test env
    assert detail["status"] == "unavailable"
    job_statuses = {j["source_name"]: j["status"] for j in detail["jobs"]}
    assert job_statuses == {"wikidata": "unavailable", "public_web": "unavailable"}

    report_resp = client.get(f"/api/v1/investigations/{investigation_id}/report")
    assert report_resp.status_code == 200
    report = report_resp.json()
    assert set(report["sources"]["unavailable"]) == {"wikidata", "public_web"}
    assert "limitations" in report and len(report["limitations"]) > 0

    health_resp = client.get("/api/v1/sources/health")
    assert health_resp.status_code == 200
    health_by_name = {h["source_name"]: h for h in health_resp.json()}
    assert health_by_name["public_web"]["installed"] is False
    assert health_by_name["wikidata"]["installed"] is False


def test_get_unknown_investigation_returns_404(client: TestClient):
    resp = client.get("/api/v1/investigations/does-not-exist")
    assert resp.status_code == 404


def test_update_notes(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "example.com"})
    investigation_id = resp.json()["id"]

    resp = client.put(f"/api/v1/investigations/{investigation_id}/notes", json={"analyst_notes": "checked manually"})
    assert resp.status_code == 200
    assert resp.json()["id"] == investigation_id

    detail = client.get(f"/api/v1/investigations/{investigation_id}").json()
    assert detail["analyst_notes"] == "checked manually"


def test_graph_and_timeline_endpoints(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    investigation_id = resp.json()["id"]
    _wait_for_terminal_status(client, investigation_id)

    graph_resp = client.get(f"/api/v1/investigations/{investigation_id}/graph")
    assert graph_resp.status_code == 200
    graph = graph_resp.json()
    assert graph["investigation_id"] == investigation_id
    assert any(n["is_root"] for n in graph["nodes"])

    timeline_resp = client.get(f"/api/v1/investigations/{investigation_id}/timeline")
    assert timeline_resp.status_code == 200
    timeline = timeline_resp.json()
    assert timeline[0]["event_type"] == "investigation_created"
    assert any(e["event_type"].startswith("job_") for e in timeline)
    # sorted chronologically
    assert timeline == sorted(timeline, key=lambda e: e["timestamp"])


def test_quick_mode_skips_public_web_and_pivots(client: TestClient, monkeypatch):
    from osint_app import orchestrator

    async def fake_investigate(db, investigation, entity, identifier_type, normalized_value, depth, start_time, mode_cfg):
        assert mode_cfg.run_public_web is False
        assert mode_cfg.adapter_limit == 1
        assert mode_cfg.max_pivot_depth == -1
        assert mode_cfg.mine_documents is False
        return []

    monkeypatch.setattr(orchestrator, "_investigate_identifier", fake_investigate)

    resp = client.post("/api/v1/investigations", json={"input_identifier": "test@example.com", "mode": "quick"})
    assert resp.status_code == 201
    assert resp.json()["mode"] == "quick"
    _wait_for_terminal_status(client, resp.json()["id"])


def test_request_id_header_present_on_every_response(client: TestClient):
    resp = client.get("/health")
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}


def test_request_id_echoed_back_when_supplied(client: TestClient):
    resp = client.get("/health", headers={"X-Request-ID": "my-correlation-id"})
    assert resp.headers["x-request-id"] == "my-correlation-id"


def test_structured_error_shape_on_404(client: TestClient):
    resp = client.get("/api/v1/investigations/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == 404
    assert "request_id" in body["error"]


def test_structured_error_shape_on_validation_failure(client: TestClient):
    resp = client.post("/api/v1/investigations", json={})  # missing required input_identifier
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == 422
    assert isinstance(body["error"]["details"], list)


def test_ready_endpoint(client: TestClient):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_max_concurrent_investigations_returns_429(client: TestClient, monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "max_concurrent_investigations", 0)
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    assert resp.status_code == 429


def test_status_endpoint_reports_job_and_pivot_counts(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    investigation_id = resp.json()["id"]
    _wait_for_terminal_status(client, investigation_id)

    status_resp = client.get(f"/api/v1/investigations/{investigation_id}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["id"] == investigation_id
    assert body["status"] == "unavailable"
    assert body["jobs"]["total"] == 2  # wikidata + public_web, both unavailable in the test env
    assert body["jobs"]["unavailable"] == 2
    assert body["elapsed_seconds"] >= 0
    assert body["cancel_requested"] is False


def test_status_endpoint_404_for_unknown_investigation(client: TestClient):
    resp = client.get("/api/v1/investigations/does-not-exist/status")
    assert resp.status_code == 404


def test_cancel_endpoint_sets_flag_and_get_reflects_it(client: TestClient, monkeypatch):
    # slow the worker down so we can observe a mid-flight cancel deterministically
    import asyncio

    from osint_app import orchestrator

    original = orchestrator._investigate_identifier

    async def slow_investigate(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_investigate_identifier", slow_investigate)

    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    investigation_id = resp.json()["id"]

    cancel_resp = client.post(f"/api/v1/investigations/{investigation_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["cancel_requested"] is True

    detail = _wait_for_terminal_status(client, investigation_id, timeout=15.0)
    assert detail["status"] == "cancelled"
    assert detail["cancel_requested"] is True


def test_cancel_already_terminal_investigation_returns_409(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    investigation_id = resp.json()["id"]
    _wait_for_terminal_status(client, investigation_id)

    cancel_resp = client.post(f"/api/v1/investigations/{investigation_id}/cancel")
    assert cancel_resp.status_code == 409


def test_cancel_unknown_investigation_returns_404(client: TestClient):
    resp = client.post("/api/v1/investigations/does-not-exist/cancel")
    assert resp.status_code == 404


def test_source_health_exposes_extended_fields(client: TestClient):
    resp = client.post("/api/v1/investigations", json={"input_identifier": "Jane Doe"})
    _wait_for_terminal_status(client, resp.json()["id"])

    health_resp = client.get("/api/v1/sources/health")
    assert health_resp.status_code == 200
    for row in health_resp.json():
        assert "success_count" in row
        assert "failure_count" in row
        assert "timeout_count" in row
