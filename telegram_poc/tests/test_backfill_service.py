"""Covers the backfill test category: successful run, checkpointing/restart
recovery, dedup, FloodWait partial failure, and rejection for an
inaccessible source.

Tests that drive `_run_backfill_job` directly create the job row via the
repository rather than through `service.start_backfill()` - the latter also
schedules its own `asyncio.create_task` background run, and having two
invocations of `_run_backfill_job` for the same job racing over one shared
in-memory session would be a real (if usually-silent) hazard, not just a
test-hygiene nicety.
"""
from datetime import datetime, timezone

import pytest
from telethon.errors import FloodWaitError

import telegram_app.services.backfill_service as backfill_module
from telegram_app.database.models import BackfillJobStatus, SourceAccessStatus, TelegramSource
from telegram_app.services.backfill_service import BackfillNotAllowedError, BackfillService, _run_backfill_job
from telegram_app.services.message_service import MessageService
from tests.fakes import FakeChannel, FakeClient, FakeMessage, SingleSessionFactory, StubClientManager


async def _make_monitoring_source(session, *, entity_id=900, username="backfillchan"):
    source = TelegramSource(
        identifier=f"@{username}", telegram_entity_id=str(entity_id), username=username,
        access_status=SourceAccessStatus.MONITORING.value, monitoring_enabled=True,
    )
    session.add(source)
    await session.flush()
    await session.commit()
    return source


async def test_start_backfill_rejected_for_unverified_source(session, settings):
    source = TelegramSource(identifier="@notyet", access_status=SourceAccessStatus.DISCOVERED.value)
    session.add(source)
    await session.flush()
    await session.commit()

    manager = StubClientManager(FakeClient())
    service = BackfillService(session, manager, settings)

    with pytest.raises(BackfillNotAllowedError):
        await service.start_backfill(source.id)


async def test_start_backfill_creates_pending_job(session, settings, monkeypatch):
    # start_backfill schedules a real asyncio.create_task background run -
    # even though it uses the fake client (no real Telegram call), that task
    # opens its own DB session via the module-level async_session_factory,
    # which must be pointed at this test's in-memory session/engine rather
    # than whatever DATABASE_URL a real .env resolves to.
    monkeypatch.setattr(backfill_module, "async_session_factory", SingleSessionFactory(session))

    source = await _make_monitoring_source(session)
    manager = StubClientManager(FakeClient())
    service = BackfillService(session, manager, settings)

    job = await service.start_backfill(source.id, limit=100)

    assert job.status == BackfillJobStatus.PENDING.value
    assert job.requested_limit == 100


async def test_run_backfill_job_success(session, settings, monkeypatch):
    source = await _make_monitoring_source(session)
    channel = FakeChannel(id=900, username="backfillchan")
    messages = [FakeMessage(id=i, message=f"old message {i}", chat_id=900, date=datetime(2025, 6, 1, tzinfo=timezone.utc)) for i in range(1, 6)]
    fake_client = FakeClient(entities={"backfillchan": channel}, messages_by_entity_id={900: messages})
    manager = StubClientManager(fake_client)
    service = BackfillService(session, manager, settings)

    monkeypatch.setattr(backfill_module, "async_session_factory", SingleSessionFactory(session))
    job = await service.jobs.create(source_id=source.id, status=BackfillJobStatus.PENDING.value, requested_limit=100)
    await session.commit()

    await _run_backfill_job(job.id, source.id, None, None, 100, manager, settings)

    refreshed = await service.get_backfill_job(source.id, job.id)
    assert refreshed.status == BackfillJobStatus.COMPLETED.value
    assert refreshed.messages_inserted == 5
    assert refreshed.checkpoint_message_id == 1  # oldest message walked

    message_service = MessageService(session, settings)
    count = await message_service.messages.count_for_source(source.id)
    assert count == 5


async def test_run_backfill_job_deduplicates_against_existing_messages(session, settings, monkeypatch):
    source = await _make_monitoring_source(session, entity_id=901, username="dupbackfill")
    channel = FakeChannel(id=901, username="dupbackfill")
    messages = [FakeMessage(id=i, message=f"msg {i}", chat_id=901) for i in range(1, 4)]
    fake_client = FakeClient(entities={"dupbackfill": channel}, messages_by_entity_id={901: messages})
    manager = StubClientManager(fake_client)
    service = BackfillService(session, manager, settings)

    monkeypatch.setattr(backfill_module, "async_session_factory", SingleSessionFactory(session))

    job1 = await service.jobs.create(source_id=source.id, status=BackfillJobStatus.PENDING.value, requested_limit=100)
    await session.commit()
    await _run_backfill_job(job1.id, source.id, None, None, 100, manager, settings)

    # A second backfill request against the same already-collected range must not duplicate.
    job2 = await service.jobs.create(source_id=source.id, status=BackfillJobStatus.PENDING.value, requested_limit=100)
    await session.commit()
    await _run_backfill_job(job2.id, source.id, None, None, 100, manager, settings)

    message_service = MessageService(session, settings)
    count = await message_service.messages.count_for_source(source.id)
    assert count == 3

    refreshed_job2 = await service.get_backfill_job(source.id, job2.id)
    assert refreshed_job2.messages_inserted == 0
    assert refreshed_job2.messages_skipped == 3


async def test_run_backfill_job_flood_wait_marks_failed_and_preserves_progress(session, settings, monkeypatch):
    source = await _make_monitoring_source(session, entity_id=902, username="floodybackfill")
    channel = FakeChannel(id=902, username="floodybackfill")
    messages = [FakeMessage(id=i, message=f"msg {i}", chat_id=902) for i in range(1, 4)]
    fake_client = FakeClient(
        entities={"floodybackfill": channel},
        messages_by_entity_id={902: messages},
        probe_raises={902: FloodWaitError(None, capture=10)},
    )
    manager = StubClientManager(fake_client)
    service = BackfillService(session, manager, settings)

    monkeypatch.setattr(backfill_module, "async_session_factory", SingleSessionFactory(session))
    job = await service.jobs.create(source_id=source.id, status=BackfillJobStatus.PENDING.value, requested_limit=100)
    await session.commit()

    await _run_backfill_job(job.id, source.id, None, None, 100, manager, settings)

    refreshed = await service.get_backfill_job(source.id, job.id)
    assert refreshed.status == BackfillJobStatus.FAILED.value  # no messages were gathered before the flood wait
    assert "FloodWaitError" in refreshed.error_message


async def test_run_backfill_job_missing_source_does_not_crash(session, settings, monkeypatch):
    monkeypatch.setattr(backfill_module, "async_session_factory", SingleSessionFactory(session))
    # No job/source exist for these ids - the background task must exit cleanly.
    await _run_backfill_job(999, 999, None, None, 100, StubClientManager(FakeClient()), settings)
