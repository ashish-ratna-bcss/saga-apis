import pytest
from telethon.errors import FloodWaitError, PremiumAccountRequiredError
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest
from telethon.tl.functions.messages import SearchGlobalRequest

from telegram_app.telegram.discovery.global_search import search_global
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeMessage, FakeSearchPostsFlood, FakeSearchResult


async def test_empty_query_rejected():
    client = FakeClient()
    with pytest.raises(TelegramSearchError) as exc_info:
        await search_global(client, "   ")
    assert exc_info.value.code == "INVALID_QUERY"


async def test_uses_channels_search_posts_when_quota_available():
    chat = FakeChannel(id=1, title="News", username="news")
    result = FakeSearchResult(messages=[FakeMessage(id=1, message="a real attack happened", chat_id=1)], chats=[chat])
    client = FakeClient(
        rpc_responses={
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(remains=5, total_daily=5, stars_amount=0),
            SearchPostsRequest: result,
        }
    )

    outcome = await search_global(client, "attack")

    assert outcome.method_used == "channels.searchPosts"
    assert len(outcome.items) == 1
    assert outcome.items[0].visibility == "public"
    assert outcome.items[0].collection_method == "global_search"
    assert outcome.items[0].channel.username == "news"


async def test_falls_back_to_search_global_when_quota_exhausted():
    chat = FakeChannel(id=1, title="News", username="news")
    fallback_result = FakeSearchResult(messages=[FakeMessage(id=1, message="attack reported", chat_id=1)], chats=[chat])
    client = FakeClient(
        rpc_responses={
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(remains=0, total_daily=5, stars_amount=50),
            SearchGlobalRequest: fallback_result,
        }
    )

    outcome = await search_global(client, "attack")

    assert outcome.method_used == "messages.searchGlobal"
    assert outcome.items[0].visibility == "account_scoped"
    assert any("free quota exhausted" in w for w in outcome.warnings)
    assert client.call_count(SearchPostsRequest) == 0, "must never call the paid method without explicit authorization"


async def test_falls_back_when_search_posts_premium_required():
    chat = FakeChannel(id=1, title="News", username="news")
    fallback_result = FakeSearchResult(messages=[FakeMessage(id=1, message="attack reported", chat_id=1)], chats=[chat])
    client = FakeClient(
        rpc_raises={CheckSearchPostsFloodRequest: PremiumAccountRequiredError(None)},
        rpc_responses={SearchGlobalRequest: fallback_result},
    )

    outcome = await search_global(client, "attack")

    assert outcome.method_used == "messages.searchGlobal"


async def test_search_global_local_keyword_reverification():
    chat = FakeChannel(id=1, title="News", username="news")
    messages = [
        FakeMessage(id=1, message="a real attack happened today", chat_id=1),
        FakeMessage(id=2, message="totally unrelated text", chat_id=1),
    ]
    result = FakeSearchResult(messages=messages, chats=[chat])
    client = FakeClient(
        rpc_raises={CheckSearchPostsFloodRequest: PremiumAccountRequiredError(None)},
        rpc_responses={SearchGlobalRequest: result},
    )

    outcome = await search_global(client, "attack")

    assert len(outcome.items) == 1
    assert "attack" in outcome.items[0].text


async def test_no_results():
    client = FakeClient(
        rpc_responses={
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(remains=5, total_daily=5, stars_amount=0),
            SearchPostsRequest: FakeSearchResult(messages=[]),
        }
    )

    outcome = await search_global(client, "zzzz_no_such_keyword")

    assert outcome.items == []
    assert outcome.has_more is False


async def test_flood_wait_propagates_as_real_exception():
    client = FakeClient(rpc_raises={CheckSearchPostsFloodRequest: FloodWaitError(None, capture=15)})

    with pytest.raises(FloodWaitError):
        await search_global(client, "attack")


async def test_pagination_cursor_produced_when_more_results_likely():
    chat = FakeChannel(id=1, title="News", username="news")
    messages = [FakeMessage(id=i, message="attack", chat_id=1) for i in range(1, 6)]
    result = FakeSearchResult(messages=messages, chats=[chat])
    client = FakeClient(
        rpc_responses={
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(remains=5, total_daily=5, stars_amount=0),
            SearchPostsRequest: result,
        }
    )

    outcome = await search_global(client, "attack", limit=5)

    assert outcome.has_more is True
    assert outcome.next_cursor is not None


async def test_media_type_filter_applies_locally_on_fallback():
    chat = FakeChannel(id=1, title="News", username="news")
    messages = [
        FakeMessage(id=1, message="attack photo", chat_id=1, media_kind="photo"),
        FakeMessage(id=2, message="attack text only", chat_id=1),
    ]
    result = FakeSearchResult(messages=messages, chats=[chat])
    client = FakeClient(
        rpc_raises={CheckSearchPostsFloodRequest: PremiumAccountRequiredError(None)},
        rpc_responses={SearchGlobalRequest: result},
    )

    outcome = await search_global(client, "attack", media_type="photo")

    assert len(outcome.items) == 1
    assert outcome.items[0].media.media_type == "photo"
