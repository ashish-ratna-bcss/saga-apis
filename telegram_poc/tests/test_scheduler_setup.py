"""Verifies the no-overlap guarantee is actually wired up: every job is
registered with max_instances=1, so APScheduler itself refuses to run a
second instance of the same job while one is still executing."""
from telegram_app.scheduler.jobs import setup_scheduler
from tests.fakes import FakeClient, StubClientManager


def test_all_jobs_registered_with_max_instances_one(settings):
    manager = StubClientManager(FakeClient())
    scheduler = setup_scheduler(manager, settings)

    jobs = scheduler.get_jobs()
    expected_ids = {
        "reconcile_pending_access",
        "run_collection_cycle",
        "connection_health_check",
        "process_notifications",
    }
    assert {job.id for job in jobs} == expected_ids
    for job in jobs:
        assert job.max_instances == 1, f"{job.id} must not allow overlapping runs"


def test_reconciliation_job_interval_is_twelve_hours_by_default(settings):
    manager = StubClientManager(FakeClient())
    scheduler = setup_scheduler(manager, settings)
    job = scheduler.get_job("reconcile_pending_access")
    assert job.trigger.interval.total_seconds() == 12 * 3600
