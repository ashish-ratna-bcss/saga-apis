"""Covers app/telegram/bot_interaction.py in isolation: sends exactly one
/start, waits bounded for the bot's reply, extracts button URLs, and never
sends anything beyond /start."""
import pytest
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError

from telegram_app.telegram.bot_interaction import start_bot
from tests.fakes import FakeClient, FakeMessage, FakeUser, make_url_button_markup


def _bot_entity(id=500, username="somebot"):
    return FakeUser(id=id, username=username, first_name="Some Bot", bot=True)


async def test_start_bot_sends_start_and_returns_reply():
    bot = _bot_entity()
    reply = FakeMessage(id=0, message="Thanks for using me!")
    fake_client = FakeClient(entities={"somebot": bot}, bot_auto_replies={500: reply})

    outcome = await start_bot(fake_client, bot, timeout_seconds=5, poll_interval_seconds=0.01)

    assert outcome.sent is True
    assert outcome.timed_out is False
    assert outcome.error is None
    assert outcome.response_text == "Thanks for using me!"

    send_calls = [c for c in fake_client.calls if c[0] == "send_message"]
    assert len(send_calls) == 1, "must send /start exactly once"
    assert send_calls[0][2] == "/start"


async def test_start_bot_never_sends_more_than_start():
    bot = _bot_entity(id=501, username="strictbot")
    reply = FakeMessage(id=0, message="hello")
    fake_client = FakeClient(entities={"strictbot": bot}, bot_auto_replies={501: reply})

    await start_bot(fake_client, bot, timeout_seconds=5, poll_interval_seconds=0.01)

    sent_texts = [c[2] for c in fake_client.calls if c[0] == "send_message"]
    assert sent_texts == ["/start"], "no message beyond the literal /start command may ever be sent"


async def test_start_bot_extracts_url_buttons_from_reply():
    bot = _bot_entity(id=502, username="buttonbot")
    markup = make_url_button_markup([("Join Channel", "https://t.me/discoveredchan"), ("Visit Site", "https://example.com")])
    reply = FakeMessage(id=0, message="Pick an option", reply_markup=markup)
    fake_client = FakeClient(entities={"buttonbot": bot}, bot_auto_replies={502: reply})

    outcome = await start_bot(fake_client, bot, timeout_seconds=5, poll_interval_seconds=0.01)

    assert len(outcome.buttons) == 2
    urls = {b.url for b in outcome.buttons}
    assert "https://t.me/discoveredchan" in urls
    assert "https://example.com" in urls  # extraction is neutral here - classification/filtering happens downstream


async def test_start_bot_times_out_when_no_reply():
    bot = _bot_entity(id=503, username="silentbot")
    fake_client = FakeClient(entities={"silentbot": bot})  # no bot_auto_replies configured

    outcome = await start_bot(fake_client, bot, timeout_seconds=0.05, poll_interval_seconds=0.01)

    assert outcome.sent is True
    assert outcome.timed_out is True
    assert outcome.response_text is None


async def test_start_bot_flood_wait_on_send_is_reported_not_retried():
    bot = _bot_entity(id=504, username="floodybot")
    fake_client = FakeClient(entities={"floodybot": bot}, send_raises={504: FloodWaitError(None, capture=30)})

    outcome = await start_bot(fake_client, bot, timeout_seconds=5, poll_interval_seconds=0.01)

    assert outcome.sent is False
    assert "FloodWaitError" in outcome.error
    send_calls = [c for c in fake_client.calls if c[0] == "send_message"]
    assert len(send_calls) == 1, "must not retry a FloodWait automatically"


async def test_start_bot_session_invalid_on_send_is_reported():
    bot = _bot_entity(id=505, username="revokedbot")
    fake_client = FakeClient(entities={"revokedbot": bot}, send_raises={505: AuthKeyUnregisteredError(None)})

    outcome = await start_bot(fake_client, bot, timeout_seconds=5, poll_interval_seconds=0.01)

    assert outcome.sent is False
    assert outcome.session_invalid is True


async def test_start_bot_ignores_own_outgoing_message_while_polling():
    """Only the bot's reply (out=False) satisfies the poll - our own /start
    (out=True) sitting in the same chat must never be mistaken for it."""
    bot = _bot_entity(id=506, username="echobot")
    fake_client = FakeClient(entities={"echobot": bot})  # no auto-reply

    outcome = await start_bot(fake_client, bot, timeout_seconds=0.05, poll_interval_seconds=0.01)

    assert outcome.timed_out is True, "our own sent message must not satisfy the wait-for-reply condition"
