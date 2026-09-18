"""Fake Telethon-shaped test doubles.

Mirrors the pattern already used by poc/tests/test_main.py:
real Telethon exception types are raised/returned, but no network call is
ever made. Only the surface area app/telegram/* actually uses is faked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from telethon.tl.types import PeerChannel

from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.collector import classify_media

_FILTER_TYPE_MAP = {
    "InputMessagesFilterPhotos": "photo",
    "InputMessagesFilterVideo": "video",
    "InputMessagesFilterDocument": "document",
    "InputMessagesFilterVoice": "voice",
    "InputMessagesFilterMusic": "audio",
}


class FakeFile:
    def __init__(self, name=None, mime_type=None, size=None):
        self.name = name
        self.mime_type = mime_type
        self.size = size


class FakeReplies:
    def __init__(self, replies=0):
        self.replies = replies


@dataclass
class FakeSender:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None


class FakeMessage:
    def __init__(
        self,
        id,
        sender_id=1,
        date=None,
        edit_date=None,
        message="",
        views=None,
        forwards=None,
        replies=None,
        grouped_id=None,
        media_kind=None,
        file=None,
        sender=None,
        chat_id=1,
        sender_username=None,
        peer_id=None,
        out=False,
        reply_markup=None,
        reply_to_msg_id=None,
    ):
        self.id = id
        self.sender_id = sender_id
        self.date = date or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.edit_date = edit_date
        self.message = message
        self.views = views
        self.forwards = forwards
        self.replies = replies
        self.grouped_id = grouped_id
        self._media_kind = media_kind
        self._file = file
        self._sender = sender
        self.chat_id = chat_id
        self.sender_username = sender_username or (getattr(sender, "username", None) if sender else None)
        # True for a message this account itself sent (e.g. the /start we
        # just sent) - bot_interaction.start_bot uses this to tell "our own
        # outgoing message" apart from "the bot's reply" while polling.
        self.out = out
        # A real telethon ReplyInlineMarkup (see make_url_button_markup
        # below) - None for a message with no buttons.
        self.reply_markup = reply_markup
        # Set for a message that is itself a reply/comment - lets FakeClient.
        # iter_messages(..., reply_to=X) find "replies to message X" the same
        # way messages.getReplies does for a channel's linked discussion group.
        self.reply_to_msg_id = reply_to_msg_id
        # Used by global-search normalization to look up which channel a
        # result came from - real Telegram search results carry this on
        # every message; per-channel collection doesn't need it.
        self.peer_id = peer_id or PeerChannel(channel_id=chat_id)
        self.photo = self._doc_or_none("photo")
        self.document = self._doc_or_none("document")

    def _doc_or_none(self, kind):
        return object() if self._media_kind == kind else None

    @property
    def voice(self):
        return self._media_kind == "voice"

    @property
    def video(self):
        return self._media_kind == "video"

    @property
    def audio(self):
        return self._media_kind == "audio"

    @property
    def sticker(self):
        return self._media_kind == "sticker"

    @property
    def gif(self):
        return self._media_kind == "gif"

    @property
    def file(self):
        return self._file

    async def get_sender(self):
        return self._sender

    def to_dict(self):
        return {"_": "Message", "id": self.id, "message": self.message, "date": self.date}


def FakeChannel(
    id,
    title=None,
    username=None,
    broadcast=True,
    megagroup=False,
    left=True,
    access_hash=0,
    verified=None,
    participants_count=None,
):
    """Builds a REAL telethon.tl.types.Channel, not a lookalike.

    access_manager/discovery classify entities via isinstance() checks
    against the real TL types (exactly as production code does against
    Telegram's real responses) - a plain dataclass double would silently
    fail every isinstance() check and be classified as UNKNOWN, masking
    real bugs. See make_chat_invite() above for the same reasoning.
    """
    from telethon.tl.types import Channel

    return Channel(
        id=id,
        title=title,
        photo=None,
        date=None,
        broadcast=broadcast,
        megagroup=megagroup,
        left=left,
        username=username,
        access_hash=access_hash,
        verified=verified,
        participants_count=participants_count,
    )


def FakeChat(id, title=None):
    """A real telethon.tl.types.Chat - see FakeChannel's docstring."""
    from telethon.tl.types import Chat

    return Chat(id=id, title=title, photo=None, participants_count=0, date=None, version=0)


def FakeUser(id, username=None, first_name=None, last_name=None, phone=None, access_hash=0, bot=False):
    """A real telethon.tl.types.User - see FakeChannel's docstring.

    Pass bot=True to build a bot account - access_manager.classify_entity_type
    checks the real `bot` field exactly like Telegram's own response would
    carry it, so a lookalike without this field would silently mask a bot-
    detection bug rather than exercise it.
    """
    from telethon.tl.types import User

    return User(
        id=id, username=username, first_name=first_name, last_name=last_name, phone=phone, access_hash=access_hash,
        bot=bot,
    )


def make_url_button_markup(buttons: list[tuple[str, str]]):
    """A real telethon ReplyInlineMarkup with one row of URL buttons - see
    FakeChannel's docstring for why real TL types are used instead of
    lookalikes. `buttons` is a list of (text, url) pairs."""
    from telethon.tl.types import KeyboardButtonRow, KeyboardButtonUrl, ReplyInlineMarkup

    return ReplyInlineMarkup(
        rows=[KeyboardButtonRow(buttons=[KeyboardButtonUrl(text=text, url=url) for text, url in buttons])]
    )


class FakeSearchResult:
    """Mirrors telethon's messages.MessagesSlice - the shape SearchGlobalRequest,
    SearchPostsRequest, and CheckChatInviteRequest-adjacent search calls return."""

    def __init__(self, messages, chats=None, users=None, next_rate=None):
        self.messages = messages
        self.chats = chats or []
        self.users = users or []
        self.next_rate = next_rate


class FakeSearchPostsFlood:
    """Mirrors telethon.tl.types.SearchPostsFlood, returned by channels.checkSearchPostsFlood."""

    def __init__(self, total_daily=5, remains=5, stars_amount=0, query_is_free=False, wait_till=None):
        self.total_daily = total_daily
        self.remains = remains
        self.stars_amount = stars_amount
        self.query_is_free = query_is_free
        self.wait_till = wait_till


class FakeContactsFound:
    """Mirrors telethon.tl.types.contacts.Found, returned by contacts.search."""

    def __init__(self, chats=None, users=None, my_results=None, results=None):
        self.chats = chats or []
        self.users = users or []
        self.my_results = my_results or []
        self.results = results or []


def make_chat_invite(title="Invite", request_needed=False, megagroup=False):
    """A real telethon ChatInvite (not-yet-a-member preview) - access_manager
    does an isinstance() check against the real TL type, so fakes must be
    real instances, not lookalikes."""
    from telethon.tl.types import ChatInvite

    return ChatInvite(title=title, photo=None, participants_count=0, color=0, request_needed=request_needed, megagroup=megagroup)


def make_chat_invite_already(chat):
    from telethon.tl.types import ChatInviteAlready

    return ChatInviteAlready(chat=chat)


class FakeClient:
    """Stands in for telethon.TelegramClient across app/telegram/* modules."""

    def __init__(
        self,
        *,
        entities: dict | None = None,
        entity_raises: dict | None = None,
        messages_by_entity_id: dict | None = None,
        probe_raises: dict | None = None,
        rpc_responses: dict | None = None,
        rpc_raises: dict | None = None,
        authorized: bool = True,
        me: FakeUser | None = None,
        download_content: bytes = b"fake-bytes",
        bot_auto_replies: dict | None = None,
        send_raises: dict | None = None,
        profile_photos: dict | None = None,
        profile_photo_raises: dict | None = None,
        message_media: dict | None = None,
        message_media_raises: dict | None = None,
    ):
        self.entities = entities or {}
        self.entity_raises = entity_raises or {}
        self.messages_by_entity_id = messages_by_entity_id or {}
        self.probe_raises = probe_raises or {}
        self.rpc_responses = rpc_responses or {}
        self.rpc_raises = rpc_raises or {}
        self._connected = False
        self._authorized = authorized
        self._me = me or FakeUser(id=999, username="me_user", phone="+15551234567")
        self.calls = []
        self.download_content = download_content
        self.pending_code_hash = "fakehash"
        # entity.id -> a FakeMessage delivered as "the bot's reply" the
        # instant send_message() is called for that entity - lets
        # bot_interaction.start_bot's poll loop resolve on its very first
        # check, with no real sleep, in the common (bot replies) test case.
        self.bot_auto_replies = bot_auto_replies or {}
        # entity.id -> exception raised by send_message() for that entity.
        self.send_raises = send_raises or {}
        self._next_sent_message_id = 100_000
        # entity.id -> raw JPEG bytes (or None, meaning "no photo") returned
        # by download_profile_photo(); entity.id -> exception it raises.
        self.profile_photos = profile_photos or {}
        self.profile_photo_raises = profile_photo_raises or {}
        # message.id -> raw bytes (or None) returned by download_media(...,
        # file=bytes); message.id -> exception it raises instead.
        self.message_media = message_media or {}
        self.message_media_raises = message_media_raises or {}

    async def connect(self):
        self._connected = True

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self._connected = False

    async def is_user_authorized(self):
        return self._authorized

    async def get_me(self):
        return self._me

    async def get_entity(self, key):
        self.calls.append(("get_entity", key))
        if key in self.entity_raises:
            raise self.entity_raises[key]
        if key in self.entities:
            return self.entities[key]
        raise KeyError(f"no fake entity registered for {key!r}")

    async def get_messages(self, entity, limit=1, ids=None):
        self.calls.append(("get_messages", entity, limit, ids))
        if entity.id in self.probe_raises:
            raise self.probe_raises[entity.id]
        if ids is not None:
            by_id = {m.id: m for m in self.messages_by_entity_id.get(entity.id, [])}
            if isinstance(ids, list):
                return [by_id.get(i) for i in ids]
            return by_id.get(ids)
        return self.messages_by_entity_id.get(entity.id, [])[:limit]

    async def iter_messages(
        self,
        entity,
        min_id=0,
        limit=100,
        reverse=True,
        search=None,
        filter=None,
        offset_id=0,
        offset_date=None,
        from_user=None,
        reply_to=None,
    ):
        self.calls.append(("iter_messages", entity, min_id, limit, search, offset_id))
        if entity.id in self.probe_raises:
            raise self.probe_raises[entity.id]

        msgs = list(self.messages_by_entity_id.get(entity.id, []))
        if reply_to is not None:
            msgs = [m for m in msgs if m.reply_to_msg_id == reply_to]
        if search:
            msgs = [m for m in msgs if search.lower() in (m.message or "").lower()]
        if from_user:
            msgs = [m for m in msgs if m.sender_username == from_user]
        if filter is not None:
            wanted_type = _FILTER_TYPE_MAP.get(type(filter).__name__)
            if wanted_type:
                msgs = [m for m in msgs if classify_media(m) == wanted_type]
        if offset_date:
            msgs = [m for m in msgs if m.date <= offset_date]
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        if min_id:
            msgs = [m for m in msgs if m.id > min_id]

        msgs = sorted(msgs, key=lambda m: m.id, reverse=not reverse)
        msgs = msgs[:limit]
        for m in msgs:
            yield m

    async def __call__(self, request):
        self.calls.append(("rpc", request))
        cls = type(request)
        if cls in self.rpc_raises:
            raise self.rpc_raises[cls]
        if cls in self.rpc_responses:
            return self.rpc_responses[cls]
        raise AssertionError(f"FakeClient got unexpected RPC request type: {cls.__name__}")

    def call_count(self, request_cls) -> int:
        return sum(1 for c in self.calls if c[0] == "rpc" and isinstance(c[1], request_cls))

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        if entity.id in self.send_raises:
            raise self.send_raises[entity.id]

        msg_id = self._next_sent_message_id
        self._next_sent_message_id += 1
        sent = FakeMessage(id=msg_id, message=text, chat_id=entity.id, sender_id=self._me.id, out=True)
        self.messages_by_entity_id.setdefault(entity.id, []).append(sent)

        reply = self.bot_auto_replies.get(entity.id)
        if reply is not None:
            reply.id = max(reply.id, msg_id + 1)
            reply.chat_id = entity.id
            self.messages_by_entity_id[entity.id].append(reply)
        return sent

    async def send_code_request(self, phone):
        return type("Sent", (), {"phone_code_hash": self.pending_code_hash})()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            return self._me
        return self._me

    async def download_media(self, message, file=None):
        self.calls.append(("download_media", message, file))
        if file is bytes:
            if message.id in self.message_media_raises:
                raise self.message_media_raises[message.id]
            return self.message_media.get(message.id)
        if file:
            with open(file, "wb") as fh:
                fh.write(self.download_content)
            return file
        return None

    async def download_profile_photo(self, entity, file=None, download_big=True):
        self.calls.append(("download_profile_photo", entity, file, download_big))
        if entity.id in self.profile_photo_raises:
            raise self.profile_photo_raises[entity.id]
        return self.profile_photos.get(entity.id)


class StubClientManager(TelegramClientManager):
    """A TelegramClientManager that wraps a pre-built FakeClient instead of
    constructing a real Telethon client - used by every service-layer test.

    Mirrors the real TelegramClientManager's session-invalid latch
    (mark_session_invalid/verify_authorized/...) so tests can exercise that
    path too, e.g. by passing a FakeClient whose entity_raises/rpc_raises
    include one of telethon's SESSION_INVALID_ERRORS.
    """

    def __init__(self, fake_client: FakeClient, configured: bool = True):
        self._fake_client = fake_client
        self._configured = configured
        self._connected_at = None
        self._last_error = None
        self._session_invalid = False
        self._session_invalid_reason = None
        self.pending_phone = None
        self.pending_phone_code_hash = None

    @property
    def is_configured(self):
        return self._configured

    @property
    def client(self):
        return self._fake_client

    async def connect(self):
        if not self._configured:
            return False, "not configured"
        await self._fake_client.connect()
        return True, None

    async def disconnect(self):
        await self._fake_client.disconnect()

    def is_connected(self):
        return self._fake_client.is_connected()

    @property
    def session_invalid(self):
        return self._session_invalid

    @property
    def session_invalid_reason(self):
        return self._session_invalid_reason

    def mark_session_invalid(self, reason: str) -> None:
        self._session_invalid = True
        self._session_invalid_reason = reason

    def clear_session_invalid(self) -> None:
        self._session_invalid = False
        self._session_invalid_reason = None

    async def is_authenticated(self):
        if self._session_invalid:
            return False
        return await self._fake_client.is_user_authorized()

    async def verify_authorized(self):
        if self._session_invalid:
            return False, self._session_invalid_reason
        ok, err = await self.connect()
        if not ok:
            return False, err
        if not await self.is_authenticated():
            # Mirrors TelegramClientManager.verify_authorized(): latch here
            # too, so a not-authorized FakeClient behaves like the real
            # manager instead of silently re-checking (and never latching)
            # on every subsequent call.
            if not self._session_invalid:
                self.mark_session_invalid("is_user_authorized() returned False")
            return False, self._session_invalid_reason
        return True, None

    async def ensure_connected(self):
        return await self.connect()


class SingleSessionFactory:
    """Makes a scheduler-job-shaped `async with async_session_factory() as session:`
    call site reuse the test's own in-memory session/engine, instead of
    opening a brand-new (empty) database - used by monkeypatch.setattr(...,
    "async_session_factory", SingleSessionFactory(session)) wherever a
    background job/task owns its own session (scheduler jobs, backfill)."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False
