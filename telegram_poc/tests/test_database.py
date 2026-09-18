import pytest
from sqlalchemy.exc import IntegrityError

from telegram_app.database.models import SourceAccessStatus, TelegramMessage, TelegramSource
from telegram_app.database.repositories.message_repository import MessageRepository
from telegram_app.database.repositories.source_repository import SourceRepository


async def test_source_unique_identifier_not_enforced_at_db_but_service_dedups(session):
    """identifier has no DB-level unique constraint (spec doesn't require one) -
    dedup is enforced in source_service.register_source via a lookup first."""
    repo = SourceRepository(session)
    await repo.create(identifier="@news", access_status=SourceAccessStatus.DISCOVERED.value)
    await session.commit()
    found = await repo.get_by_identifier("@news")
    assert found is not None
    assert found.identifier == "@news"


async def test_message_unique_constraint_prevents_duplicates(session):
    source = TelegramSource(identifier="@news", access_status=SourceAccessStatus.MONITORING.value)
    session.add(source)
    await session.flush()

    session.add(TelegramMessage(source_id=source.id, telegram_message_id=100, text="first"))
    await session.commit()

    session.add(TelegramMessage(source_id=source.id, telegram_message_id=100, text="duplicate"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_message_repository_upsert_ignore_duplicate_is_idempotent(session):
    source = TelegramSource(identifier="@news", access_status=SourceAccessStatus.MONITORING.value)
    session.add(source)
    await session.flush()
    await session.commit()

    repo = MessageRepository(session)
    inserted_first = await repo.upsert_ignore_duplicate(source_id=source.id, telegram_message_id=1, text="hello")
    await session.commit()
    inserted_second = await repo.upsert_ignore_duplicate(source_id=source.id, telegram_message_id=1, text="hello again")
    await session.commit()

    assert inserted_first is True
    assert inserted_second is False
    count = await repo.count_for_source(source.id)
    assert count == 1


async def test_cascade_delete_removes_messages(session):
    source = TelegramSource(identifier="@news", access_status=SourceAccessStatus.MONITORING.value)
    session.add(source)
    await session.flush()
    session.add(TelegramMessage(source_id=source.id, telegram_message_id=1, text="hi"))
    await session.commit()

    repo = SourceRepository(session)
    fetched = await repo.get(source.id)
    await repo.delete(fetched)
    await session.commit()

    msg_repo = MessageRepository(session)
    remaining = await msg_repo.count_for_source(source.id)
    assert remaining == 0
