from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from osint_app import orchestrator
from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.db import Base
from osint_app.enums import ClaimType, EntityType, IdentifierType, JobStatus
from osint_app.models import Entity, Investigation, SearchJob, SourceHealth


class FakeAdapter(SourceAdapter):
    accepts = EntityType.USERNAME

    def __init__(self, name: str, *, available: bool = True, outcome: str = "found"):
        self.name = name
        self._available = available
        self._outcome = outcome  # "found" | "empty" | "unavailable" | "error"

    async def is_available(self) -> bool:
        return self._available

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        if self._outcome == "unavailable":
            raise SourceUnavailable("simulated unavailable")
        if self._outcome == "error":
            raise RuntimeError("simulated bug")
        if self._outcome == "empty":
            return []
        return [
            AdapterResult(
                source=self.name,
                query=normalized_identifier,
                entity_type=EntityType.SOCIAL_PROFILE,
                value=f"https://example.com/{normalized_identifier}",
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.PUBLIC_ASSOCIATION,
                confidence=0.55,
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(url="https://example.com", title="t", metadata={}),
                extraction_method="fake",
            )
        ]


class FakePublicWebAdapter(SourceAdapter):
    name = "public_web"
    accepts = None

    async def is_available(self) -> bool:
        return False

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        raise SourceUnavailable("no searxng configured")

    async def run_queries(self, queries: list[str]) -> list[AdapterResult]:
        raise SourceUnavailable("no searxng configured")


@pytest.fixture()
def test_sessionmaker():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    from osint_app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_investigation(Session) -> str:
    db = Session()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123"
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()
    return inv_id


@pytest.mark.asyncio
async def test_successful_adapter_run_ingests_and_completes(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeAdapter("fake_source")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    investigation = db.get(Investigation, inv_id)
    jobs = db.query(SearchJob).filter_by(investigation_id=inv_id).all()
    entities = db.query(Entity).filter_by(investigation_id=inv_id).all()

    job_by_source = {j.source_name: j for j in jobs}
    assert job_by_source["fake_source"].status == JobStatus.COMPLETED
    assert job_by_source["fake_source"].result_count == 1
    assert job_by_source["public_web"].status == JobStatus.UNAVAILABLE
    assert investigation.status == JobStatus.PARTIAL  # one completed, one unavailable
    assert len(entities) == 2  # root Username + discovered SocialProfile

    health = db.get(SourceHealth, "fake_source")
    assert health.success_count == 1
    assert health.installed is True

    web_health = db.get(SourceHealth, "public_web")
    assert web_health.installed is False
    db.close()


@pytest.mark.asyncio
async def test_unavailable_adapter_marks_job_and_health(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeAdapter("flaky", outcome="unavailable")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="flaky").one()
    assert job.status == JobStatus.UNAVAILABLE
    assert "simulated unavailable" in job.error

    investigation = db.get(Investigation, inv_id)
    assert investigation.status == JobStatus.UNAVAILABLE  # both jobs unavailable
    db.close()


@pytest.mark.asyncio
async def test_adapter_bug_produces_failed_job_not_crashed_worker(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(
        orchestrator, "adapters_for", lambda t: [FakeAdapter("buggy", outcome="error"), FakeAdapter("good")]
    )
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)  # must not raise

    db = test_sessionmaker()
    jobs = {j.source_name: j for j in db.query(SearchJob).filter_by(investigation_id=inv_id).all()}
    assert jobs["buggy"].status == JobStatus.FAILED
    assert jobs["good"].status == JobStatus.COMPLETED

    investigation = db.get(Investigation, inv_id)
    assert investigation.status == JobStatus.PARTIAL
    db.close()


@pytest.mark.asyncio
async def test_not_installed_adapter_skips_run_and_marks_unavailable(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    calls = []

    class NeverCalled(FakeAdapter):
        async def run(self, normalized_identifier):
            calls.append("run")
            return await super().run(normalized_identifier)

    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [NeverCalled("uninstalled", available=False)])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    assert calls == []  # run() must never be called when is_available() is False
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="uninstalled").one()
    assert job.status == JobStatus.UNAVAILABLE
    health = db.get(SourceHealth, "uninstalled")
    assert health.installed is False
    db.close()


class FakeUrlProducingAdapter(FakeAdapter):
    """Produces a URL result so document_mining has something to select."""

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        return [
            AdapterResult(
                source=self.name,
                query=normalized_identifier,
                entity_type=EntityType.URL,
                value="https://example.com/report.pdf",
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.PUBLIC_ASSOCIATION,
                confidence=0.4,
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(url="https://example.com/report.pdf", title="t", metadata={}),
                extraction_method="fake",
            )
        ]


class FakeDocumentAdapter(SourceAdapter):
    name = "document_extraction"
    accepts = None

    def __init__(self):
        self.calls: list[str] = []

    async def is_available(self) -> bool:
        return True

    async def run(self, url: str) -> list[AdapterResult]:
        self.calls.append(url)
        return [
            AdapterResult(
                source=self.name,
                query=url,
                entity_type=EntityType.DOCUMENT,
                value=url,
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.PUBLIC_ASSOCIATION,
                confidence=1.0,
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(url=url, title="Document", metadata={"snippet": ""}),
                extraction_method="document_fetch",
            )
        ]


@pytest.mark.asyncio
async def test_deep_mode_triggers_document_mining(monkeypatch, test_sessionmaker):
    from osint_app.config import settings

    fake_doc_adapter = FakeDocumentAdapter()
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeUrlProducingAdapter("fake_source")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())
    monkeypatch.setattr(orchestrator, "DOCUMENT_ADAPTER", fake_doc_adapter)
    # Isolate this test to "does deep mode trigger document mining at all" --
    # pivoting is exercised separately and would otherwise recurse into the
    # same FakeUrlProducingAdapter for a pivoted identifier too, mining the
    # same URL a second time and making this test about recursion, not mode gating.
    monkeypatch.setattr(settings, "pivot_enabled", False)

    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
        mode="deep",
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    assert fake_doc_adapter.calls == ["https://example.com/report.pdf"]
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="document_extraction").one()
    assert job.status == JobStatus.COMPLETED
    db.close()


@pytest.mark.asyncio
async def test_standard_mode_does_not_trigger_document_mining(monkeypatch, test_sessionmaker):
    fake_doc_adapter = FakeDocumentAdapter()
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeUrlProducingAdapter("fake_source")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())
    monkeypatch.setattr(orchestrator, "DOCUMENT_ADAPTER", fake_doc_adapter)

    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
        mode="standard",
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    assert fake_doc_adapter.calls == []
    db = test_sessionmaker()
    assert db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="document_extraction").count() == 0
    db.close()


@pytest.mark.asyncio
async def test_quick_mode_does_not_trigger_document_mining(monkeypatch, test_sessionmaker):
    fake_doc_adapter = FakeDocumentAdapter()
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeUrlProducingAdapter("fake_source")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())
    monkeypatch.setattr(orchestrator, "DOCUMENT_ADAPTER", fake_doc_adapter)

    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
        mode="quick",
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    assert fake_doc_adapter.calls == []


class FlakyThenSucceedsAdapter(SourceAdapter):
    """Fails with a retryable SourceUnavailable `fail_times` times, then succeeds."""

    accepts = EntityType.USERNAME

    def __init__(self, name: str, *, fail_times: int, retryable: bool = True):
        self.name = name
        self._fail_times = fail_times
        self._retryable = retryable
        self.call_count = 0

    async def is_available(self) -> bool:
        return True

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise SourceUnavailable("simulated transient failure", retryable=self._retryable)
        return []


@pytest.mark.asyncio
async def test_transient_failure_is_retried_and_eventually_succeeds(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    from osint_app.config import settings

    monkeypatch.setattr(settings, "adapter_max_retries", 2)
    monkeypatch.setattr(settings, "adapter_retry_backoff_seconds", 0.001)

    flaky = FlakyThenSucceedsAdapter("flaky", fail_times=2)  # fails twice, succeeds on 3rd (final) attempt
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [flaky])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    assert flaky.call_count == 3
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="flaky").one()
    assert job.status == JobStatus.COMPLETED
    db.close()


@pytest.mark.asyncio
async def test_retries_exhausted_still_marks_unavailable(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    from osint_app.config import settings

    monkeypatch.setattr(settings, "adapter_max_retries", 2)
    monkeypatch.setattr(settings, "adapter_retry_backoff_seconds", 0.001)

    flaky = FlakyThenSucceedsAdapter("always_fails", fail_times=999)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [flaky])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    assert flaky.call_count == 3  # 1 initial + 2 retries, then gives up
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="always_fails").one()
    assert job.status == JobStatus.UNAVAILABLE
    health = db.get(SourceHealth, "always_fails")
    assert health.failure_count == 3
    db.close()


@pytest.mark.asyncio
async def test_permanent_failure_is_never_retried(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    from osint_app.config import settings

    monkeypatch.setattr(settings, "adapter_max_retries", 2)
    monkeypatch.setattr(settings, "adapter_retry_backoff_seconds", 0.001)

    blocked = FlakyThenSucceedsAdapter("blocked", fail_times=999, retryable=False)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [blocked])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    assert blocked.call_count == 1  # no retries at all -- retryable=False, a permanent-block style failure
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="blocked").one()
    assert job.status == JobStatus.UNAVAILABLE
    db.close()


@pytest.mark.asyncio
async def test_unexpected_exception_is_never_retried(monkeypatch, test_sessionmaker):
    """A bare-Exception bug (FAILED, not UNAVAILABLE) must not be retried --
    retrying a programming error blindly could mask or worsen it."""
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    from osint_app.config import settings

    monkeypatch.setattr(settings, "adapter_max_retries", 2)

    buggy = FakeAdapter("buggy", outcome="error")
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [buggy])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)  # must not raise

    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="buggy").one()
    assert job.status == JobStatus.FAILED
    health = db.get(SourceHealth, "buggy")
    assert health.run_count == 1  # exactly one attempt, no retry loop for bugs
    db.close()


@pytest.mark.asyncio
async def test_disabled_source_health_skips_dispatch(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    calls = []

    class TrackedAdapter(FakeAdapter):
        async def run(self, normalized_identifier):
            calls.append("run")
            return await super().run(normalized_identifier)

    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [TrackedAdapter("disabled_source")])

    db = test_sessionmaker()
    inv = Investigation(input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123")
    db.add(inv)
    db.add(SourceHealth(source_name="disabled_source", enabled=False, run_count=0, success_count=0, failure_count=0, timeout_count=0, total_duration_seconds=0.0))
    db.commit()
    inv_id = inv.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    assert calls == []  # run() never called -- source is operator-disabled
    db = test_sessionmaker()
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="disabled_source").one()
    assert job.status == JobStatus.UNAVAILABLE
    assert "disabled" in job.error
    db.close()


@pytest.mark.asyncio
async def test_timeout_error_increments_source_health_timeout_count(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    from osint_app.config import settings

    monkeypatch.setattr(settings, "adapter_max_retries", 0)

    class TimesOutAdapter(SourceAdapter):
        name = "slow_source"
        accepts = EntityType.USERNAME

        async def is_available(self) -> bool:
            return True

        async def run(self, normalized_identifier: str) -> list[AdapterResult]:
            try:
                raise TimeoutError("deadline exceeded")
            except TimeoutError as exc:
                raise SourceUnavailable("slow_source scan exceeded overall time budget") from exc

    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [TimesOutAdapter()])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    inv_id = _make_investigation(test_sessionmaker)
    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    health = db.get(SourceHealth, "slow_source")
    assert health.timeout_count == 1
    db.close()


@pytest.mark.asyncio
async def test_cancellation_stops_new_work_and_marks_cancelled(monkeypatch, test_sessionmaker):
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [FakeAdapter("fake_source")])
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
        cancel_requested=True,
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    investigation = db.get(Investigation, inv_id)
    assert investigation.status == JobStatus.CANCELLED
    # cancellation was already requested before the worker even started --
    # no jobs should have been created at all
    assert db.query(SearchJob).filter_by(investigation_id=inv_id).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_cancellation_mid_investigation_finalizes_as_cancelled(monkeypatch, test_sessionmaker):
    """Cancellation requested *after* the root's own adapters already ran --
    those jobs complete normally (cooperative, not abrupt), but no pivots
    execute and the final status reflects the cancellation."""
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())

    class CancelDuringRun(FakeAdapter):
        async def run(self, normalized_identifier):
            # simulate a cancel request arriving while this adapter is running,
            # via a separate session -- exactly how the real API would do it
            db2 = test_sessionmaker()
            inv = db2.get(Investigation, self._inv_id)
            inv.cancel_requested = True
            db2.commit()
            db2.close()
            return await super().run(normalized_identifier)

    adapter = CancelDuringRun("fake_source")
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [adapter])

    inv_id = _make_investigation(test_sessionmaker)
    adapter._inv_id = inv_id

    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    investigation = db.get(Investigation, inv_id)
    assert investigation.status == JobStatus.CANCELLED
    # the root-level adapter job that was already in flight still completed cleanly
    job = db.query(SearchJob).filter_by(investigation_id=inv_id, source_name="fake_source").one()
    assert job.status == JobStatus.COMPLETED
    db.close()


@pytest.mark.asyncio
async def test_source_concurrency_capped_across_investigations(test_sessionmaker, monkeypatch):
    """STEP 10: N investigations pivoting into the same source concurrently
    must never exceed settings.source_max_concurrency in-flight calls."""
    import asyncio

    monkeypatch.setattr(orchestrator.settings, "source_max_concurrency", 2)
    orchestrator._source_semaphores.clear()

    in_flight = 0
    peak = 0

    class SlowAdapter(FakeAdapter):
        async def run(self, normalized_identifier):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return await super().run(normalized_identifier)

    adapter = SlowAdapter("slow_source")
    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
    )
    db.add(inv)
    db.commit()
    entity = Entity(
        investigation_id=inv.id, entity_type=EntityType.USERNAME, value="rahul_123",
        normalized_value="rahul_123", claim_type=ClaimType.PUBLIC_ASSOCIATION, confidence=1.0,
    )
    db.add(entity)
    db.commit()

    await asyncio.gather(
        *[
            orchestrator._run_adapter_job(db, inv, adapter, lambda: adapter.run("rahul_123"), entity)
            for _ in range(5)
        ]
    )
    db.close()

    assert peak <= 2


@pytest.mark.asyncio
async def test_orphaned_running_job_from_crashed_worker_is_reaped_and_investigation_completes(
    monkeypatch, test_sessionmaker
):
    """Reproduces the exact bug caught live in Postgres crash-recovery
    testing: a worker crashes mid-adapter-call, leaving its SearchJob row
    stuck RUNNING; a second run of process_investigation (simulating another
    worker reclaiming the investigation) must close out that orphan instead
    of letting it block a clean terminal status forever."""
    monkeypatch.setattr(orchestrator, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(orchestrator, "PUBLIC_WEB_ADAPTER", FakePublicWebAdapter())
    adapter = FakeAdapter("fake_source")
    monkeypatch.setattr(orchestrator, "adapters_for", lambda t: [adapter])

    inv_id = _make_investigation(test_sessionmaker)

    db = test_sessionmaker()
    orphan = SearchJob(investigation_id=inv_id, source_name="crashed_source", status=JobStatus.RUNNING)
    db.add(orphan)
    db.commit()
    orphan_id = orphan.id
    db.close()

    await orchestrator.process_investigation(inv_id)

    db = test_sessionmaker()
    investigation = db.get(Investigation, inv_id)
    assert investigation.status == JobStatus.PARTIAL  # the real, live-run job succeeded; the orphan is a real failure
    reaped = db.get(SearchJob, orphan_id)
    assert reaped.status == JobStatus.FAILED
    assert reaped.finished_at is not None
    db.close()


def test_can_transition_state_machine():
    from osint_app.enums import can_transition

    assert can_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    assert can_transition(JobStatus.RUNNING, JobStatus.COMPLETED)
    assert can_transition(JobStatus.RUNNING, JobStatus.QUEUED)  # lease reclaim
    assert can_transition(JobStatus.COMPLETED, JobStatus.COMPLETED)  # idempotent re-write
    assert not can_transition(JobStatus.COMPLETED, JobStatus.RUNNING)
    assert not can_transition(JobStatus.CANCELLED, JobStatus.COMPLETED)
    assert not can_transition(JobStatus.QUEUED, JobStatus.COMPLETED)


def test_set_status_refuses_to_overwrite_terminal_status(test_sessionmaker):
    """Simulates the exact race STEP 6 guards against: a worker's lease gets
    reclaimed and another worker completes the investigation before the
    original (slow, still-alive) worker gets around to writing its own
    result -- that stale write must be dropped, not silently applied."""
    db = test_sessionmaker()
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
        status=JobStatus.RUNNING,
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id

    # a second worker (different session) already finished it
    db2 = test_sessionmaker()
    other = db2.get(Investigation, inv_id)
    other.status = JobStatus.COMPLETED
    db2.commit()
    db2.close()

    # the original (stale) session still thinks it's RUNNING and tries to write FAILED
    wrote = orchestrator._set_status(db, inv, JobStatus.FAILED)
    assert wrote is False
    assert inv.status == JobStatus.COMPLETED  # refresh() picked up the real, terminal status
    db.close()
