"""
Unit tests for main.py's search modes.

Runs with either `python -m unittest discover` or `pytest` - no extra test
dependencies required (uses stdlib unittest.IsolatedAsyncioTestCase + mock).

Telegram is never contacted: a FakeClient stands in for TelegramClient and
raises/returns exactly the Telethon RPC types the real client would produce,
so these tests exercise main.py's own branching logic, not Telethon or the
network.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.errors import (  # noqa: E402
    ChannelPrivateError,
    FloodWaitError,
    PremiumAccountRequiredError,
    RPCError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest  # noqa: E402
from telethon.tl.functions.messages import SearchGlobalRequest  # noqa: E402
from telethon.tl.types import PeerChannel  # noqa: E402

import main  # noqa: E402


# --- Test fixtures/doubles ----------------------------------------------------

class FakeMessage:
    def __init__(self, id, peer_id=None, message="", date=None, sender_id=1, reply_to=None, media=None):
        self.id = id
        self.peer_id = peer_id
        self.message = message
        self.date = date
        self.sender_id = sender_id
        self.reply_to = reply_to
        self.media = media


class FakeChat:
    def __init__(self, id, title, username=None):
        self.id = id
        self.title = title
        self.username = username


class FakeSearchResult:
    def __init__(self, messages, chats=None, users=None):
        self.messages = messages
        self.chats = chats or []
        self.users = users or []


class FakeSearchPostsFlood:
    """Mirrors telethon.tl.types.SearchPostsFlood, returned by channels.checkSearchPostsFlood."""

    def __init__(self, total_daily=5, remains=5, stars_amount=0, query_is_free=False, wait_till=None):
        self.total_daily = total_daily
        self.remains = remains
        self.stars_amount = stars_amount
        self.query_is_free = query_is_free
        self.wait_till = wait_till


def free_quota_flood():
    """Quota check result meaning: plenty of free slots left, no payment needed."""
    return FakeSearchPostsFlood(total_daily=5, remains=5, stars_amount=0)


def exhausted_quota_flood(stars_amount=50, wait_till=1999999999):
    """Quota check result meaning: free slots used up, this query costs Stars."""
    return FakeSearchPostsFlood(total_daily=5, remains=0, stars_amount=stars_amount, wait_till=wait_till)


class FakeClient:
    """Stands in for TelegramClient. Dispatches on the awaited request's type."""

    def __init__(self, responses=None, raises=None):
        self.responses = dict(responses or {})
        self.raises = dict(raises or {})
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        cls = type(request)
        if cls in self.raises:
            raise self.raises[cls]
        if cls in self.responses:
            return self.responses[cls]
        raise AssertionError(f"FakeClient got an unexpected request type: {cls.__name__}")

    def call_count(self, request_cls):
        return sum(1 for c in self.calls if isinstance(c, request_cls))


@contextlib.contextmanager
def captured_stdout():
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        yield buf


class TempOutputDirMixin:
    """Redirects main.OUTPUT_DIR to a throwaway temp dir for the duration of a test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = patch.object(main, "OUTPUT_DIR", Path(self._tmpdir.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()


# --- Local keyword verification -----------------------------------------------

class TestMessageMatchesKeyword(unittest.TestCase):
    def test_literal_substring_match(self):
        self.assertTrue(main.message_matches_keyword({"text": "an attack happened"}, "attack"))

    def test_case_insensitive(self):
        self.assertTrue(main.message_matches_keyword({"text": "ATTACK reported"}, "attack"))

    def test_no_match(self):
        self.assertFalse(main.message_matches_keyword({"text": "nothing relevant here"}, "attack"))

    def test_missing_text_field(self):
        self.assertFalse(main.message_matches_keyword({"text": None}, "attack"))


# --- Peer resolution / serialization (existing behavior, unchanged) -----------

class TestSerializeGlobalMessage(unittest.TestCase):
    def test_resolves_channel_title_and_username(self):
        chats_by_id, users_by_id = main.build_entity_index(
            [FakeChat(id=42, title="Example Channel", username="example")], []
        )
        msg = FakeMessage(id=7, peer_id=PeerChannel(channel_id=42), message="hello attack world")
        serialized = main.serialize_global_message(msg, chats_by_id, users_by_id)
        self.assertEqual(serialized["channel_username"], "example")
        self.assertEqual(serialized["channel_title"], "Example Channel")
        self.assertEqual(serialized["message_url"], "https://t.me/example/7")


# --- Result ordering / transparency about search scope ------------------------

class TestSortLatestFirst(unittest.TestCase):
    def test_sorts_most_recent_first_regardless_of_input_order(self):
        messages = [
            {"date": "2024-01-01T00:00:00+00:00"},
            {"date": "2026-06-15T00:00:00+00:00"},
            {"date": "2025-03-10T00:00:00+00:00"},
        ]
        sorted_messages = main.sort_latest_first(messages)
        self.assertEqual(
            [m["date"] for m in sorted_messages],
            ["2026-06-15T00:00:00+00:00", "2025-03-10T00:00:00+00:00", "2024-01-01T00:00:00+00:00"],
        )

    def test_messages_with_no_date_sort_last(self):
        messages = [{"date": None}, {"date": "2026-01-01T00:00:00+00:00"}]
        sorted_messages = main.sort_latest_first(messages)
        self.assertEqual(sorted_messages[0]["date"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(sorted_messages[1]["date"])


class TestDistinctChannelsSummary(unittest.TestCase):
    def test_counts_and_lists_unique_channels(self):
        messages = [
            {"channel_username": "news", "channel_title": "News"},
            {"channel_username": "news", "channel_title": "News"},
            {"channel_username": "deals", "channel_title": "Deals"},
            {"channel_username": None, "channel_title": "No Username Chat"},
        ]
        count, listed = main.distinct_channels_summary(messages)
        self.assertEqual(count, 3)
        self.assertIn("@news", listed)
        self.assertIn("@deals", listed)
        self.assertIn("No Username Chat", listed)


# --- --public-search: true method, Premium restriction, fallback --------------

class TestPublicSearchTrueMethod(TempOutputDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_success_does_not_trigger_fallback(self):
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="attack reported")], [chat])
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: free_quota_flood(), SearchPostsRequest: result}
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        self.assertEqual(client.call_count(SearchGlobalRequest), 0, "must not fall back on success")
        text = out.getvalue()
        self.assertIn("RESULT: PUBLIC POST SEARCH CONTENT RECEIVED", text)
        self.assertNotIn("Telegram Stars", text, "a free (unpaid) search must not claim a Stars payment")

    async def test_zero_results_is_not_an_error(self):
        result = FakeSearchResult([], [])
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: free_quota_flood(), SearchPostsRequest: result}
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "zzzz_no_such_keyword")

        text = out.getvalue()
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT", text)
        self.assertIn("no public channel post currently matches", text)

    async def test_quota_check_failure_falls_through_to_direct_attempt(self):
        """If the free channels.checkSearchPostsFlood diagnostic itself fails, still try the real search."""
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="attack reported")], [chat])

        class SomeOtherRpcError(RPCError):
            def __init__(self):
                self.request = None

        client = FakeClient(
            raises={CheckSearchPostsFloodRequest: SomeOtherRpcError()},
            responses={SearchPostsRequest: result},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 1, "must still attempt the real search")
        self.assertIn("Could not check the free search quota first", text)
        self.assertIn("RESULT: PUBLIC POST SEARCH CONTENT RECEIVED", text)


class TestPublicSearchPremiumFallback(TempOutputDirMixin, unittest.IsolatedAsyncioTestCase):
    def _client_with_premium_restriction(self, global_search_result):
        """A free quota check passes, but the actual channels.searchPosts call is still rejected.

        This covers the case where Telegram grants a free-search quota check but the account is
        otherwise ineligible to use the method at all (e.g. never had access to the feature).
        """
        return FakeClient(
            raises={SearchPostsRequest: PremiumAccountRequiredError(None)},
            responses={CheckSearchPostsFloodRequest: free_quota_flood(), SearchGlobalRequest: global_search_result},
        )

    async def test_premium_restriction_triggers_automatic_fallback(self):
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="an attack was reported")], [chat])
        client = self._client_with_premium_restriction(result)

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 1)
        self.assertEqual(client.call_count(SearchGlobalRequest), 1, "fallback must actually call messages.searchGlobal")
        self.assertIn("channels.searchPosts UNAVAILABLE", text)
        self.assertIn("PREMIUM_ACCOUNT_REQUIRED", text)
        self.assertIn("Falling back to: messages.searchGlobal", text)
        self.assertIn("RESULT: FALLBACK SEARCH CONTENT RECEIVED", text)
        self.assertNotIn("RESULT: PUBLIC POST SEARCH CONTENT RECEIVED", text, "fallback must not be mislabeled as the true method")

    async def test_no_fallback_flag_disables_automatic_fallback(self):
        client = self._client_with_premium_restriction(FakeSearchResult([]))

        with captured_stdout() as out:
            await main.do_public_search(client, "attack", allow_fallback=False)

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchGlobalRequest), 0, "--no-fallback must prevent the fallback call")
        self.assertIn("channels.searchPosts UNAVAILABLE", text)
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT", text)
        self.assertIn("--no-fallback", text)

    async def test_fallback_zero_results(self):
        client = self._client_with_premium_restriction(FakeSearchResult([]))

        with captured_stdout() as out:
            await main.do_public_search(client, "zzzz_no_such_keyword")

        text = out.getvalue()
        self.assertIn("RESULT: FALLBACK SEARCH RETURNED NO CONTENT", text)
        self.assertIn("naturally empty", text)

    async def test_fallback_locally_re_verifies_keyword_matches(self):
        """messages.searchGlobal can return fuzzy/token matches; only literal hits should count."""
        chat = FakeChat(id=1, title="News", username="news")
        messages = [
            FakeMessage(id=1, peer_id=PeerChannel(1), message="a real attack happened today"),
            FakeMessage(id=2, peer_id=PeerChannel(1), message="totally unrelated text, no keyword here"),
        ]
        client = self._client_with_premium_restriction(FakeSearchResult(messages, [chat]))

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertIn("Telegram's global index returned 2 candidate message(s)", text)
        self.assertIn("1 contain the literal keyword", text)
        self.assertIn("RESULT: FALLBACK SEARCH CONTENT RECEIVED", text)

    async def test_fallback_warns_about_account_scoped_index(self):
        """The user-reported concern: fallback results look limited to 'my subscribed channels'."""
        chat = FakeChat(id=1, title="News", username="news")
        messages = [FakeMessage(id=1, peer_id=PeerChannel(1), message="attack reported")]
        client = self._client_with_premium_restriction(FakeSearchResult(messages, [chat]))

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertIn("Drawn from 1 distinct channel(s)", text)
        self.assertIn("channels this account has joined/can already see", text)
        self.assertIn("not an unrestricted crawl of every", text)

    async def test_fallback_results_are_sorted_most_recent_first(self):
        from datetime import datetime, timezone

        chat = FakeChat(id=1, title="News", username="news")
        messages = [
            FakeMessage(id=1, peer_id=PeerChannel(1), message="old attack news", date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            FakeMessage(id=2, peer_id=PeerChannel(1), message="newest attack news", date=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        client = self._client_with_premium_restriction(FakeSearchResult(messages, [chat]))

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertLess(text.index("newest attack news"), text.index("old attack news"))


class TestPublicSearchErrorHandling(TempOutputDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_is_reported_distinctly_and_does_not_fall_back(self):
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: free_quota_flood()},
            raises={SearchPostsRequest: FloodWaitError(None, capture=30)},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchGlobalRequest), 0, "FloodWait must not trigger the Premium fallback")
        self.assertIn("FloodWaitError", text)
        self.assertIn("30s", text)
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT", text)

    async def test_generic_rpc_error_is_reported_with_its_own_class_name(self):
        class SomeOtherRpcError(RPCError):
            def __init__(self):
                self.request = None

        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: free_quota_flood()},
            raises={SearchPostsRequest: SomeOtherRpcError()},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchGlobalRequest), 0)
        self.assertIn("SomeOtherRpcError", text, "specific RPC error must be named, not swallowed as generic 'no content'")
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT", text)

    async def test_network_error_is_reported_distinctly(self):
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: free_quota_flood()},
            raises={SearchPostsRequest: ConnectionError("connection refused")},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertIn("network/connection error", text)
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT", text)


# --- --public-search: real free-quota / Telegram Stars payment model ----------

class TestPublicSearchQuotaAndPayment(TempOutputDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_flood_check_reports_premium_required_skips_direct_attempt(self):
        """If even the free quota check is rejected, don't bother calling the real method - go straight to fallback."""
        client = FakeClient(
            raises={CheckSearchPostsFloodRequest: PremiumAccountRequiredError(None)},
            responses={SearchGlobalRequest: FakeSearchResult([])},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 0, "must not attempt the paid/free call after a hard rejection")
        self.assertEqual(client.call_count(SearchGlobalRequest), 1)
        self.assertIn("channels.searchPosts UNAVAILABLE", text)

    async def test_query_is_free_flag_bypasses_zero_remaining_slots(self):
        """query_is_free=True means this exact query doesn't cost anything even with 0 slots left."""
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="attack reported")], [chat])
        flood = FakeSearchPostsFlood(total_daily=5, remains=0, stars_amount=50, query_is_free=True)
        client = FakeClient(responses={CheckSearchPostsFloodRequest: flood, SearchPostsRequest: result})

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 1)
        self.assertIsNone(client.calls[-1].allow_paid_stars, "a free query must not authorize any Stars payment")
        self.assertIn("RESULT: PUBLIC POST SEARCH CONTENT RECEIVED", text)

    async def test_quota_exhausted_without_payment_authorization_falls_back(self):
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: exhausted_quota_flood(stars_amount=50), SearchGlobalRequest: FakeSearchResult([])},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack")

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 0, "must not call the paid method without authorization")
        self.assertEqual(client.call_count(SearchGlobalRequest), 1)
        self.assertIn("50 Telegram Stars", text)
        self.assertIn("--pay-stars", text)

    async def test_quota_exhausted_with_insufficient_pay_stars_cap_falls_back(self):
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: exhausted_quota_flood(stars_amount=50), SearchGlobalRequest: FakeSearchResult([])},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack", pay_stars_cap=10)

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 0, "must refuse to pay more than authorized")
        self.assertEqual(client.call_count(SearchGlobalRequest), 1)
        self.assertIn("50 Telegram Stars", text)
        self.assertIn("only authorized", text)

    async def test_quota_exhausted_with_sufficient_pay_stars_cap_pays_and_succeeds(self):
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="attack reported")], [chat])
        client = FakeClient(
            responses={CheckSearchPostsFloodRequest: exhausted_quota_flood(stars_amount=50), SearchPostsRequest: result},
        )

        with captured_stdout() as out:
            await main.do_public_search(client, "attack", pay_stars_cap=100)

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchGlobalRequest), 0, "must not fall back once payment succeeds")
        self.assertEqual(client.call_count(SearchPostsRequest), 1)
        self.assertEqual(client.calls[-1].allow_paid_stars, 50, "must authorize exactly Telegram's quoted amount, not the cap")
        self.assertIn("Authorizing payment of 50 Telegram Stars", text)
        self.assertIn("RESULT: PUBLIC POST SEARCH CONTENT RECEIVED (paid 50 Telegram Stars for this search)", text)

    async def test_quota_exhausted_no_payment_no_fallback_stops_cleanly(self):
        client = FakeClient(responses={CheckSearchPostsFloodRequest: exhausted_quota_flood(stars_amount=50)})

        with captured_stdout() as out:
            await main.do_public_search(client, "attack", allow_fallback=False)

        text = out.getvalue()
        self.assertEqual(client.call_count(SearchPostsRequest), 0)
        self.assertEqual(client.call_count(SearchGlobalRequest), 0)
        self.assertIn("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT (--no-fallback set)", text)


# --- --global-search: unchanged existing behavior ------------------------------

class TestGlobalSearch(TempOutputDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        chat = FakeChat(id=1, title="News", username="news")
        result = FakeSearchResult([FakeMessage(id=1, peer_id=PeerChannel(1), message="attack news")], [chat])
        client = FakeClient(responses={SearchGlobalRequest: result})

        with captured_stdout() as out:
            await main.do_global_search(client, "attack")

        self.assertIn("RESULT: GLOBAL SEARCH CONTENT RECEIVED", out.getvalue())

    async def test_zero_results(self):
        client = FakeClient(responses={SearchGlobalRequest: FakeSearchResult([])})

        with captured_stdout() as out:
            await main.do_global_search(client, "zzzz_no_such_keyword")

        self.assertIn("RESULT: GLOBAL SEARCH RETURNED NO CONTENT", out.getvalue())

    async def test_flood_wait(self):
        client = FakeClient(raises={SearchGlobalRequest: FloodWaitError(None, capture=5)})

        with captured_stdout() as out:
            await main.do_global_search(client, "attack")

        text = out.getvalue()
        self.assertIn("FloodWaitError", text)
        self.assertIn("RESULT: GLOBAL SEARCH RETURNED NO CONTENT", text)


# --- Existing per-channel fetch/search mode: unchanged existing behavior ------

class TestFetchChannel(unittest.IsolatedAsyncioTestCase):
    async def test_private_channel_is_reported_not_swallowed(self):
        class Client:
            async def get_entity(self, channel):
                raise ChannelPrivateError(None)

        result = await main.fetch_channel(Client(), "@someprivate", None)
        self.assertFalse(result["ok"])
        self.assertIn("private", result["reason"])

    async def test_unresolvable_username(self):
        class Client:
            async def get_entity(self, channel):
                raise UsernameNotOccupiedError(None)

        result = await main.fetch_channel(Client(), "@doesnotexist", None)
        self.assertFalse(result["ok"])
        self.assertIn("could not resolve", result["reason"])

    async def test_successful_fetch_with_keyword(self):
        chat = FakeChat(id=1, title="Example", username="example")

        class Client:
            async def get_entity(self, channel):
                return chat

            async def get_messages(self, entity, limit, search):
                self.last_search = search
                return [FakeMessage(id=1, message="an attack happened")]

        client = Client()
        result = await main.fetch_channel(client, "@example", "attack")
        self.assertTrue(result["ok"])
        self.assertEqual(client.last_search, "attack")
        self.assertEqual(len(result["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
