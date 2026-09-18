"""Entity-level deduplication.

Identifier-string dedup alone cannot catch the same channel arriving under
two spellings that normalize differently - "@name" and the numeric id
normalize to "@name" and "700", but resolve to one telegram_entity_id. That
is not a hypothetical: GET /api/telegram/channels/search hands back numeric
ids, so registering a discovery hit for a channel an operator already added
by username is the ordinary path into it. Two rows for one channel means it
is collected twice, monitored twice, and counted twice against Telegram's
rate limits, with messages duplicated across both source ids.
"""
import pytest
from sqlalchemy import select
from telethon.errors import UsernameNotOccupiedError

from telegram_app.database.models import AuditLog, SourceAccessStatus, TelegramSource
from telegram_app.services.source_service import SourceAlreadyExistsError, SourceService
from tests.fakes import FakeChannel, FakeClient, StubClientManager


def _service(session, *, entity_id=700, username="dedupchan"):
    channel = FakeChannel(id=entity_id, title="Dedup Channel", username=username, broadcast=True, left=True)
    fake_client = FakeClient(
        entities={username: channel, str(entity_id): channel, entity_id: channel},
        messages_by_entity_id={entity_id: []},
    )
    return SourceService(session, StubClientManager(fake_client))


async def test_same_channel_by_username_then_numeric_id_is_rejected(session):
    service = _service(session)

    first = await service.register_source("@dedupchan")
    assert first.telegram_entity_id == "700"

    with pytest.raises(SourceAlreadyExistsError) as caught:
        await service.register_source("700")

    assert caught.value.existing_source_id == first.id

    rows = (await session.execute(select(TelegramSource))).scalars().all()
    assert [r.id for r in rows] == [first.id], "the duplicate row must not survive"


async def test_same_channel_by_numeric_id_then_username_is_rejected(session):
    """The reverse order matters too - discovery can register a channel by id
    before an operator tries to add it by name."""
    service = _service(session)

    first = await service.register_source("700")

    with pytest.raises(SourceAlreadyExistsError) as caught:
        await service.register_source("@dedupchan")

    assert caught.value.existing_source_id == first.id
    rows = (await session.execute(select(TelegramSource))).scalars().all()
    assert len(rows) == 1


async def test_rejected_duplicate_leaves_no_dangling_audit_rows(session):
    """The aborted registration's audit events are kept as history but must
    not point at the source id that was just deleted - SQLite is not run with
    foreign_keys=ON, so ON DELETE SET NULL would not fire on its own."""
    service = _service(session)

    first = await service.register_source("@dedupchan")
    with pytest.raises(SourceAlreadyExistsError):
        await service.register_source("700")

    live_ids = {r.id for r in (await session.execute(select(TelegramSource))).scalars().all()}
    audit_rows = (await session.execute(select(AuditLog))).scalars().all()

    dangling = [r for r in audit_rows if r.source_id is not None and r.source_id not in live_ids]
    assert dangling == [], f"audit rows point at deleted sources: {[(r.id, r.event_type) for r in dangling]}"

    # The rejection itself is recorded against the source that survived.
    rejections = [r for r in audit_rows if not r.success and "rejected_identifier" in (r.details or {})]
    assert len(rejections) == 1
    assert rejections[0].source_id == first.id


async def test_distinct_channels_still_register_independently(session):
    """The dedup must key on the resolved entity, not merely on both rows
    having *some* entity id - two different channels stay two sources."""
    one = FakeChannel(id=801, title="One", username="chanone", broadcast=True, left=True)
    two = FakeChannel(id=802, title="Two", username="chantwo", broadcast=True, left=True)
    fake_client = FakeClient(
        entities={"chanone": one, "chantwo": two},
        messages_by_entity_id={801: [], 802: []},
    )
    service = SourceService(session, StubClientManager(fake_client))

    a = await service.register_source("@chanone")
    b = await service.register_source("@chantwo")

    assert a.id != b.id
    assert {a.telegram_entity_id, b.telegram_entity_id} == {"801", "802"}


async def test_unresolved_sources_are_not_treated_as_duplicates(session):
    """Two sources that never resolved both carry telegram_entity_id=None.
    That must not read as 'same entity' - otherwise the second unreachable
    channel an operator adds would be silently rejected as a duplicate of the
    first."""
    fake_client = FakeClient(
        entity_raises={
            "ghostone": UsernameNotOccupiedError(None),
            "ghosttwo": UsernameNotOccupiedError(None),
        }
    )
    service = SourceService(session, StubClientManager(fake_client))

    a = await service.register_source("@ghostone")
    b = await service.register_source("@ghosttwo")

    assert a.id != b.id
    assert a.telegram_entity_id is None
    assert b.telegram_entity_id is None
    assert a.access_status != SourceAccessStatus.MONITORING.value
