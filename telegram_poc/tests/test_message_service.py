from datetime import datetime, timezone

from telegram_app.database.models import SourceAccessStatus, TelegramSource
from telegram_app.services.message_service import MessageService
from telegram_app.telegram.collector import NormalizedMessage


def _msg(tid, text="hello"):
    return NormalizedMessage(
        telegram_message_id=tid,
        sender_id="1",
        sender_username=None,
        sender_display_name=None,
        message_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        edit_date=None,
        text=text,
        views=None,
        forwards=None,
        reply_count=None,
        grouped_id=None,
        media_type=None,
        source_url=None,
        raw_data_hash=f"hash{tid}",
        raw_json={"id": tid, "message": text},
    )


async def _make_source(session):
    source = TelegramSource(identifier="@news", access_status=SourceAccessStatus.MONITORING.value)
    session.add(source)
    await session.flush()
    await session.commit()
    return source


async def test_persist_collected_messages_inserts_and_advances_checkpoint(session, settings):
    source = await _make_source(session)
    service = MessageService(session, settings)

    metrics = await service.persist_collected_messages(source.id, [_msg(1), _msg(2), _msg(3)])

    assert metrics.messages_inserted == 3
    assert metrics.messages_skipped == 0
    checkpoint = await service.checkpoints.get_or_create(source.id)
    assert checkpoint.last_message_id == 3


async def test_persist_collected_messages_is_restart_safe_no_duplicates(session, settings):
    source = await _make_source(session)
    service = MessageService(session, settings)

    await service.persist_collected_messages(source.id, [_msg(1), _msg(2)])
    # Simulate a restart re-delivering the same batch (e.g. checkpoint update
    # lagged behind a crash) - nothing should be duplicated.
    metrics = await service.persist_collected_messages(source.id, [_msg(1), _msg(2), _msg(3)])

    assert metrics.messages_inserted == 1  # only message 3 is new
    assert metrics.messages_skipped == 2
    count = await service.messages.count_for_source(source.id)
    assert count == 3


async def test_persist_collected_messages_writes_raw_evidence_file(session, settings, tmp_path):
    source = await _make_source(session)
    service = MessageService(session, settings)

    await service.persist_collected_messages(source.id, [_msg(42, text="evidence text")])

    evidence_file = tmp_path / "raw_messages" / str(source.id) / "42.json"
    assert evidence_file.exists()
    assert "evidence text" in evidence_file.read_text(encoding="utf-8")
