"""
Telegram MTProto scraping POC (Telethon).

Standalone connectivity/content-retrieval test only.
- Public channels/groups and public channel posts only.
- No proxy rotation, anti-ban, CAPTCHA bypass, or rate-limit bypass.
- No crawling/joining of channels not explicitly listed by the operator.
- --global-search / --public-search use Telegram's own global search RPCs
  directly (messages.searchGlobal / channels.searchPosts) - they do NOT loop
  over any channel list. --public-search additionally falls back to
  messages.searchGlobal (still a single direct RPC call, not a loop) when
  channels.searchPosts is unavailable to the authenticated account.
- Not connected to any other project.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PremiumAccountRequiredError,
    RPCError,
    SessionPasswordNeededError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputMessagesFilterEmpty,
    InputPeerEmpty,
    PeerChannel,
    PeerChat,
    PeerUser,
)

BASE_DIR = Path(__file__).resolve().parent
SESSION_NAME = str(BASE_DIR / "telegram_poc_session")
OUTPUT_DIR = BASE_DIR / "output"
MESSAGE_LIMIT = 100
MAX_CHANNELS_PER_RUN = 10  # deliberate cap: this stays a bounded POC, not a crawler.

logger = logging.getLogger("telegram_poc")


# --- Configuration -----------------------------------------------------------

def load_credentials():
    load_dotenv(BASE_DIR / ".env")

    raw_api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    missing = [
        name for name, value in (("TELEGRAM_API_ID", raw_api_id), ("TELEGRAM_API_HASH", api_hash)) if not value
    ]
    if missing:
        print(f"ERROR: Missing required .env values: {', '.join(missing)}")
        print("Edit .env and fill in TELEGRAM_API_ID / TELEGRAM_API_HASH.")
        sys.exit(1)

    try:
        api_id = int(raw_api_id)
    except ValueError:
        print("ERROR: TELEGRAM_API_ID must be numeric. Check your .env file.")
        sys.exit(1)

    # api_hash is intentionally never printed or logged anywhere in this script.
    return api_id, api_hash


def get_default_channel():
    channel = os.getenv("TELEGRAM_CHANNEL")
    if not channel:
        print("ERROR: TELEGRAM_CHANNEL is required in .env for this mode.")
        print("(Not needed for --global-search / --public-search.)")
        sys.exit(1)
    return channel.strip()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Telegram MTProto POC - fetch, keyword-search, or globally search public Telegram content."
    )
    parser.add_argument(
        "--search",
        metavar="KEYWORD",
        default=None,
        help="Only return messages containing this keyword, via Telegram's per-chat server-side search. "
        "Combines with the default TELEGRAM_CHANNEL or --channels.",
    )
    parser.add_argument(
        "--channels",
        metavar="CH1,CH2,...",
        default=None,
        help=f"Comma-separated list of public channel/group usernames to check instead of TELEGRAM_CHANNEL "
        f"(max {MAX_CHANNELS_PER_RUN}). Still one explicit chat lookup per name - no crawling.",
    )
    search_group = parser.add_mutually_exclusive_group()
    search_group.add_argument(
        "--global-search",
        metavar="KEYWORD",
        default=None,
        help="True Telegram-wide keyword search using messages.searchGlobal (real MTProto call, not a loop "
        "over --channels). Scoped to public broadcast channels. Ignores TELEGRAM_CHANNEL/--channels/--search.",
    )
    search_group.add_argument(
        "--public-search",
        metavar="KEYWORD",
        default=None,
        help="True global search of public channels via channels.searchPosts, including channels this account "
        "has not joined (real MTProto call). Every account gets a limited free daily quota (checked via "
        "channels.checkSearchPostsFlood); beyond that it costs Telegram Stars (see --pay-stars) or requires "
        "Premium. If unavailable, automatically falls back to messages.searchGlobal (see --no-fallback). "
        "Ignores TELEGRAM_CHANNEL/--channels/--search.",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="With --public-search only: if channels.searchPosts is unavailable (e.g. Premium required), report "
        "that plainly and stop instead of automatically falling back to messages.searchGlobal.",
    )
    parser.add_argument(
        "--pay-stars",
        metavar="MAX_STARS",
        type=int,
        default=None,
        help="With --public-search only: if this account's free daily channels.searchPosts quota (checked via "
        "channels.checkSearchPostsFlood) is exhausted, authorize paying up to MAX_STARS real Telegram Stars for "
        "this one search. Spends real money - only the exact amount Telegram quotes is charged, and only if it "
        "does not exceed MAX_STARS. Without this flag, no payment is ever attempted.",
    )
    return parser.parse_args()


def parse_channels_arg(raw):
    if not raw:
        return None
    channels = [c.strip() for c in raw.split(",") if c.strip()]
    if len(channels) > MAX_CHANNELS_PER_RUN:
        print(f"ERROR: {len(channels)} channels given, max is {MAX_CHANNELS_PER_RUN} per run.")
        print("This POC intentionally stays bounded to a short, explicit list - not a crawler.")
        sys.exit(1)
    return channels or None


# --- Shared helpers ------------------------------------------------------------

def build_message_url(username, message_id):
    if username:
        return f"https://t.me/{username}/{message_id}"
    return None


def slugify(text, max_len=40):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").lower()
    return slug[:max_len] or "search"


def build_output_path(mode, keyword=None):
    """Builds a unique per-run output path so each search gets its own JSON file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parts = [mode]
    if keyword:
        parts.append(slugify(keyword))
    parts.append(timestamp)
    return OUTPUT_DIR / ("_".join(parts) + ".json")


def write_output(serialized, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serialized, f, ensure_ascii=False, indent=2, default=str)


async def connect_and_authenticate(client):
    """Connects and ensures the session is authorized. Returns True on success.

    Interactive phone/code/2FA prompts are handled by Telethon's own
    client.start(). Prints its own RESULT line and disconnects on failure.
    """
    try:
        await client.connect()
    except ApiIdInvalidError:
        print("RESULT: NO CONTENT RECEIVED")
        print("Reason: TELEGRAM_API_ID / TELEGRAM_API_HASH was rejected by Telegram (invalid API ID or hash).")
        return False
    except (ConnectionError, OSError) as exc:
        print("RESULT: NO CONTENT RECEIVED")
        print(f"Reason: network/connection error reaching Telegram servers ({exc}).")
        print("Possible causes: no internet access, DNS/firewall blocking Telegram, or Telegram unreachable from this network.")
        return False

    try:
        if not await client.is_user_authorized():
            print("No existing session found - starting interactive login (phone number + verification code).")
            await client.start()
        else:
            print("Existing session found - skipping interactive login.")
        auth_ok = await client.is_user_authorized()
    except ApiIdInvalidError:
        print("RESULT: NO CONTENT RECEIVED")
        print("Reason: TELEGRAM_API_ID / TELEGRAM_API_HASH was rejected by Telegram (invalid API ID or hash).")
        await client.disconnect()
        return False
    except SessionPasswordNeededError:
        print("RESULT: NO CONTENT RECEIVED")
        print("Reason: account has two-factor authentication enabled and the password step did not complete.")
        await client.disconnect()
        return False
    except (PhoneNumberInvalidError, PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        print("RESULT: NO CONTENT RECEIVED")
        print(f"Reason: authentication failed during login flow ({exc.__class__.__name__}).")
        await client.disconnect()
        return False

    if not auth_ok:
        print("RESULT: NO CONTENT RECEIVED")
        print("Reason: authentication did not complete successfully.")
        await client.disconnect()
        return False

    print("Telegram connection: SUCCESS")
    print("Authentication: SUCCESS")
    print()
    return True


# --- Single-channel / multi-channel mode (--channels, --search, default) -----

def serialize_message(message, chat_title, chat_username):
    reply_to_id = None
    if message.reply_to is not None:
        reply_to_id = message.reply_to.reply_to_msg_id

    return {
        "message_id": message.id,
        "channel_title": chat_title,
        "channel_username": chat_username,
        "text": message.message or None,
        "date": message.date.astimezone(timezone.utc).isoformat() if message.date else None,
        "sender_id": message.sender_id,
        "reply_to_message_id": reply_to_id,
        "media_present": message.media is not None,
        "message_url": build_message_url(chat_username, message.id),
    }


async def fetch_channel(client, channel, search_keyword):
    """Resolve one public channel/group and fetch its messages (optionally keyword-filtered).

    Returns a dict describing the outcome. Never raises - callers loop over
    multiple channels and one failure must not abort the others.
    """
    try:
        entity = await client.get_entity(channel)
    except ChannelPrivateError:
        return {
            "channel": channel,
            "ok": False,
            "reason": "private channel/group - this account has no access (public channels only are supported).",
        }
    except (UsernameNotOccupiedError, UsernameInvalidError, ChannelInvalidError, ValueError) as exc:
        return {
            "channel": channel,
            "ok": False,
            "reason": f"could not resolve ({exc.__class__.__name__}) - misspelled username, or not public.",
        }
    except FloodWaitError as exc:
        return {
            "channel": channel,
            "ok": False,
            "reason": f"FloodWaitError while resolving - must wait {exc.seconds}s before retrying.",
        }
    except RPCError as exc:
        return {
            "channel": channel,
            "ok": False,
            "reason": f"Telegram RPC error ({exc.__class__.__name__}): {exc}",
        }

    chat_title = getattr(entity, "title", None) or channel
    chat_username = getattr(entity, "username", None)
    if isinstance(entity, Channel):
        source_type = "PUBLIC_CHANNEL" if getattr(entity, "broadcast", False) else "PUBLIC_GROUP"
    elif isinstance(entity, Chat):
        source_type = "PUBLIC_GROUP"
    else:
        source_type = "UNKNOWN"

    if search_keyword:
        logger.info(
            "search strategy: messages.search (per-chat server-side search) on %s, q=%r", channel, search_keyword
        )
    try:
        messages = await client.get_messages(entity, limit=MESSAGE_LIMIT, search=search_keyword)
    except FloodWaitError as exc:
        return {
            "channel": channel,
            "ok": False,
            "reason": f"FloodWaitError - rate-limited. Wait {exc.seconds}s before retrying. "
            "(No rate-limit bypass is implemented, by design.)",
        }
    except ChannelPrivateError:
        return {
            "channel": channel,
            "ok": False,
            "reason": "access to messages was denied (private channel).",
        }
    except RPCError as exc:
        return {
            "channel": channel,
            "ok": False,
            "reason": f"Telegram RPC error ({exc.__class__.__name__}): {exc}",
        }

    return {
        "channel": channel,
        "ok": True,
        "chat_title": chat_title,
        "chat_username": chat_username,
        "source_type": source_type,
        "messages": [serialize_message(m, chat_title, chat_username) for m in messages],
    }


async def run_channel_mode(client, channels, search_keyword):
    output_path = build_output_path("search" if search_keyword else "fetch", search_keyword)
    results = [await fetch_channel(client, ch, search_keyword) for ch in channels]

    all_messages = []
    for result in results:
        if not result["ok"]:
            print(f"Source: {result['channel']}")
            print("  -> SKIPPED")
            print(f"  -> Reason: {result['reason']}")
            continue

        msgs = result["messages"]
        text_count = sum(1 for m in msgs if m["text"])
        media_count = sum(1 for m in msgs if m["media_present"])
        label = f"@{result['chat_username']}" if result["chat_username"] else result["chat_title"]
        print(f"Source: {label}")
        print(f"  Source type: {result['source_type']}")
        print(f"  Messages received: {len(msgs)} (text: {text_count}, media: {media_count})")
        all_messages.extend(msgs)

    write_output(all_messages, output_path)

    total_received = len(all_messages)
    total_text = sum(1 for m in all_messages if m["text"])
    total_media = sum(1 for m in all_messages if m["media_present"])
    failed_channels = [r["channel"] for r in results if not r["ok"]]

    print()
    print("--- Summary " + "-" * 47)
    if search_keyword:
        print(f"Search keyword: '{search_keyword}'")
    print(f"Channels requested: {len(channels)}")
    print(f"Channels accessible: {len(channels) - len(failed_channels)}")
    print(f"Messages requested per channel: {MESSAGE_LIMIT}")
    print(f"Messages received (all channels): {total_received}")
    print(f"Messages containing text: {total_text}")
    print(f"Messages containing media: {total_media}")
    print(f"Output: output/{output_path.name}")
    print()

    if total_received == 0:
        print("RESULT: NO CONTENT RECEIVED")
        if search_keyword:
            print(f"Possible reasons: no message in the configured channel(s) matches '{search_keyword}', or")
            print("this account cannot see their message history. Reporting honestly - no bypass attempted.")
        else:
            print("Possible reasons: the channel(s) genuinely have no messages yet, this account cannot see their")
            print("message history, or they were newly created/emptied. Reporting honestly - no bypass attempted.")
    else:
        print("RESULT: CONTENT RECEIVED SUCCESSFULLY")


# --- Global search / public post search modes ---------------------------------
#
# These call Telegram's own global-scope MTProto search methods directly.
# Neither loops over --channels or TELEGRAM_CHANNEL - the "which channels"
# decision is made entirely by Telegram's servers, not by this script.

def build_entity_index(chats, users):
    return {c.id: c for c in chats}, {u.id: u for u in users}


def resolve_peer(peer, chats_by_id, users_by_id):
    if peer is None:
        return None, None
    if isinstance(peer, PeerChannel):
        entity = chats_by_id.get(peer.channel_id)
    elif isinstance(peer, PeerChat):
        entity = chats_by_id.get(peer.chat_id)
    elif isinstance(peer, PeerUser):
        entity = users_by_id.get(peer.user_id)
    else:
        entity = None
    if entity is None:
        return None, None

    title = getattr(entity, "title", None)
    if title is None:
        first = getattr(entity, "first_name", None)
        last = getattr(entity, "last_name", None)
        title = " ".join(p for p in (first, last) if p) or None
    return title, getattr(entity, "username", None)


def serialize_global_message(message, chats_by_id, users_by_id):
    peer_title, peer_username = resolve_peer(getattr(message, "peer_id", None), chats_by_id, users_by_id)

    reply_to_id = None
    if message.reply_to is not None:
        reply_to_id = message.reply_to.reply_to_msg_id

    return {
        "message_id": message.id,
        "channel_title": peer_title,
        "channel_username": peer_username,
        "text": message.message or None,
        "date": message.date.astimezone(timezone.utc).isoformat() if message.date else None,
        "sender_id": message.sender_id,
        "reply_to_message_id": reply_to_id,
        "media_present": message.media is not None,
        "message_url": build_message_url(peer_username, message.id),
    }


def message_matches_keyword(serialized_message, keyword):
    """Case-insensitive literal substring check against a serialized message's text.

    Telegram's server-side global search index (messages.searchGlobal) can be
    token/fuzzy-based rather than strict substring matching, so results are
    re-verified locally before being reported as genuine keyword matches.
    """
    text = serialized_message.get("text") or ""
    return keyword.lower() in text.lower()


async def call_search_global(client, keyword, limit=MESSAGE_LIMIT, broadcasts_only=True):
    """Direct wrapper around the messages.searchGlobal MTProto call.

    Raises Telethon/RPC exceptions to the caller - no exception handling here,
    so both --global-search and the --public-search fallback can apply their
    own labeling around the same underlying call.
    """
    return await client(
        SearchGlobalRequest(
            q=keyword,
            filter=InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=limit,
            broadcasts_only=broadcasts_only,
        )
    )


def sort_latest_first(serialized):
    """Sorts serialized messages by date, most recent first.

    Telegram's search RPCs rank by their own relevance/offset_rate, which is
    not guaranteed to be strict chronological order. This is a purely local,
    client-side sort of messages Telegram already returned - it does not
    change what was searched or matched, only the order results are reported
    in, so "latest N matches" is a reliable read of the output.
    """
    return sorted(serialized, key=lambda m: m["date"] or "", reverse=True)


def distinct_channels_summary(serialized, max_listed=15):
    """Summarizes how many distinct channels the results are drawn from.

    Useful for --public-search's fallback: a result set concentrated in a
    handful of channels is expected evidence that messages.searchGlobal (for
    a non-Premium account) is scoped to channels this account can already
    see - not proof of a bug in this script.
    """
    labels = []
    seen = set()
    for m in serialized:
        key = m["channel_username"] or m["channel_title"] or "unknown"
        if key not in seen:
            seen.add(key)
            labels.append(f"@{m['channel_username']}" if m["channel_username"] else key)
    shown = ", ".join(labels[:max_listed])
    if len(labels) > max_listed:
        shown += f", ... (+{len(labels) - max_listed} more)"
    return len(labels), shown


def print_search_results(keyword, serialized, output_path):
    print(f"Keyword: '{keyword}'")
    print(f"Results: {len(serialized)}")
    for m in serialized:
        label = f"@{m['channel_username']}" if m["channel_username"] else (m["channel_title"] or "unknown source")
        text = m["text"] or ""
        snippet = text if len(text) <= 100 else text[:100] + "..."
        print(
            f"- source={label} message_id={m['message_id']} date={m['date']} "
            f"media_present={m['media_present']} text={snippet!r}"
        )
    print(f"Output: output/{output_path.name}")
    print()


async def do_global_search(client, keyword):
    print(f"Global search keyword: '{keyword}'")
    print("Method: messages.searchGlobal (Telegram MTProto) - scope: public broadcast channels (broadcasts_only=True)")
    print()
    logger.info("search strategy: messages.searchGlobal (broadcasts_only=True), q=%r", keyword)

    try:
        result = await call_search_global(client, keyword)
    except FloodWaitError as exc:
        print("RESULT: GLOBAL SEARCH RETURNED NO CONTENT")
        print(f"Reason: FloodWaitError - Telegram is rate-limiting this account. Wait {exc.seconds}s before retrying.")
        return
    except (ConnectionError, OSError) as exc:
        print("RESULT: GLOBAL SEARCH RETURNED NO CONTENT")
        print(f"Reason: network/connection error during search ({exc}).")
        return
    except RPCError as exc:
        print("RESULT: GLOBAL SEARCH RETURNED NO CONTENT")
        print(f"Reason: Telegram RPC error ({exc.__class__.__name__}): {exc}")
        return

    chats_by_id, users_by_id = build_entity_index(result.chats, result.users)
    serialized = sort_latest_first([serialize_global_message(m, chats_by_id, users_by_id) for m in result.messages])

    output_path = build_output_path("global-search", keyword)
    write_output(serialized, output_path)
    print_search_results(keyword, serialized, output_path)

    if serialized:
        channel_count, channel_list = distinct_channels_summary(serialized)
        print(f"Drawn from {channel_count} distinct channel(s): {channel_list}")
        print(
            "Note: messages.searchGlobal's results for this account are scoped to what Telegram's global index "
            "surfaces for it - typically concentrated in channels this account already has visibility into."
        )
        print()
        print("RESULT: GLOBAL SEARCH CONTENT RECEIVED")
    else:
        print("RESULT: GLOBAL SEARCH RETURNED NO CONTENT")
        print(f"Possible reasons: no public channel message currently matches '{keyword}' in Telegram's global")
        print("search index visible to this account, or the match set is naturally empty. No bypass attempted.")


async def call_search_posts(client, keyword, limit=MESSAGE_LIMIT, allow_paid_stars=None):
    """Direct wrapper around the channels.searchPosts MTProto call.

    allow_paid_stars, if set, authorizes Telegram to charge that many real
    Telegram Stars for this call if the account's free daily quota (see
    call_check_search_posts_flood) is exhausted. Raises Telethon/RPC
    exceptions to the caller - no exception handling here.
    """
    return await client(
        SearchPostsRequest(
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=limit,
            hashtag=None,
            query=keyword,
            allow_paid_stars=allow_paid_stars,
        )
    )


async def call_check_search_posts_flood(client, keyword):
    """Direct wrapper around the channels.checkSearchPostsFlood MTProto call.

    Free, read-only: tells the caller whether a channels.searchPosts query
    for this keyword would be free (within the account's daily quota) or
    would require paying Telegram Stars, and if so, how many. Raises
    Telethon/RPC exceptions to the caller - no exception handling here.
    """
    return await client(CheckSearchPostsFloodRequest(query=keyword))


async def do_public_search_fallback(client, keyword):
    """Fallback used only when channels.searchPosts is unavailable to this account.

    NOT a re-implementation or equivalent of channels.searchPosts. It calls
    messages.searchGlobal - a different, real MTProto method that is not
    Premium-gated - restricted to public broadcast channels (broadcasts_only=
    True), which is Telegram's own account-scoped global message index rather
    than a dedicated "public post search" feature. Results are additionally
    re-verified locally (literal, case-insensitive substring match) since that
    index can return token/fuzzy matches rather than strict substring hits.
    This function performs a single direct RPC call - it does not loop over
    or crawl any channel list.
    """
    logger.info("search strategy: fallback -> messages.searchGlobal (broadcasts_only=True), q=%r", keyword)
    print("Falling back to: messages.searchGlobal (Telegram MTProto)")
    print("This fallback searches Telegram's own public-broadcast-channel message index for this account.")
    print("It is NOT channels.searchPosts and is not claimed to be an equivalent global public-post search.")
    print()

    try:
        result = await call_search_global(client, keyword)
    except FloodWaitError as exc:
        print("RESULT: FALLBACK SEARCH RETURNED NO CONTENT")
        print(f"Reason: FloodWaitError - Telegram is rate-limiting this account. Wait {exc.seconds}s before retrying.")
        return
    except (ConnectionError, OSError) as exc:
        print("RESULT: FALLBACK SEARCH RETURNED NO CONTENT")
        print(f"Reason: network/connection error during fallback search ({exc}).")
        return
    except RPCError as exc:
        print("RESULT: FALLBACK SEARCH RETURNED NO CONTENT")
        print(f"Reason: Telegram RPC error ({exc.__class__.__name__}): {exc}")
        return

    chats_by_id, users_by_id = build_entity_index(result.chats, result.users)
    candidates = [serialize_global_message(m, chats_by_id, users_by_id) for m in result.messages]
    verified = sort_latest_first([m for m in candidates if message_matches_keyword(m, keyword)])

    output_path = build_output_path("public-search-fallback", keyword)
    write_output(verified, output_path)
    print_search_results(keyword, verified, output_path)
    print(
        f"(Telegram's global index returned {len(candidates)} candidate message(s) for this account; "
        f"{len(verified)} contain the literal keyword '{keyword}' after local re-verification. "
        "Sorted most-recent-first.)"
    )

    if verified:
        channel_count, channel_list = distinct_channels_summary(verified)
        print(f"Drawn from {channel_count} distinct channel(s): {channel_list}")
        print(
            "IMPORTANT: this is messages.searchGlobal, not channels.searchPosts. For a non-Premium account it is "
            "scoped to public channels Telegram's global index surfaces for THIS account - in practice this is "
            "usually the channels this account has joined/can already see, not an unrestricted crawl of every "
            "public channel on Telegram. A concentrated result set (few distinct channels) is expected evidence "
            "of that scope, not a bug in this fallback. Only Telegram Premium's channels.searchPosts removes this "
            "restriction - see README 'Non-Premium fallback behavior'."
        )
        print()
        print("RESULT: FALLBACK SEARCH CONTENT RECEIVED")
    else:
        print()
        print("RESULT: FALLBACK SEARCH RETURNED NO CONTENT")
        print(f"Possible reasons: no public broadcast channel message indexed by Telegram for this account")
        print(f"currently contains '{keyword}', or the match set is naturally empty. No bypass attempted.")


async def do_public_search(client, keyword, allow_fallback=True, pay_stars_cap=None):
    """Runs the true global public-post search, respecting Telegram's real quota/payment model.

    channels.searchPosts (query mode) genuinely searches public channels this
    account has NOT joined - see Telegram's own docs at
    core.telegram.org/method/channels.searchPosts. It is not simply
    "Premium-only": every account gets a limited number of free full-text
    search slots per day (checked via channels.checkSearchPostsFlood,
    non-Premium-gated and free to call); once that quota is exhausted, the
    call requires either Telegram Premium or paying a per-search amount of
    real Telegram Stars via SearchPostsRequest.allow_paid_stars. This
    function never spends Stars unless the caller explicitly authorizes an
    amount via pay_stars_cap (from --pay-stars) that covers Telegram's quoted
    price - otherwise it reports the requirement plainly and, if allowed,
    falls back to do_public_search_fallback().
    """
    print(f"Public post search keyword: '{keyword}'")
    print("Method: channels.searchPosts (Telegram MTProto) - true Telegram-wide public post search")
    print("(genuinely covers public channels this account has not joined - see README)")
    print()

    async def unavailable_then_maybe_fallback(reason_lines):
        logger.info(
            "search strategy: channels.searchPosts unavailable%s",
            " -> falling back to messages.searchGlobal" if allow_fallback else " -> fallback disabled (--no-fallback)",
        )
        for line in reason_lines:
            print(line)
        print()
        if not allow_fallback:
            print("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT (--no-fallback set)")
            return
        await do_public_search_fallback(client, keyword)

    logger.info("search strategy: checking free quota via channels.checkSearchPostsFlood, q=%r", keyword)
    flood = None
    try:
        flood = await call_check_search_posts_flood(client, keyword)
    except PremiumAccountRequiredError:
        await unavailable_then_maybe_fallback(
            [
                "channels.searchPosts UNAVAILABLE for this account.",
                "Reason: Telegram rejected even the free quota check (RPC error: PREMIUM_ACCOUNT_REQUIRED) - this",
                "account cannot use this method at all right now. This is a genuine Telegram-side restriction,",
                "not a bug in this POC, and this script never bypasses it - see README.",
            ]
        )
        return
    except (RPCError, ConnectionError, OSError) as exc:
        logger.warning(
            "channels.checkSearchPostsFlood failed (%s) - attempting channels.searchPosts directly", exc.__class__.__name__
        )
        print(f"(Could not check the free search quota first: {exc.__class__.__name__}: {exc} - attempting the search directly.)")
        print()

    stars_needed = 0
    if flood is not None:
        is_free = getattr(flood, "query_is_free", False) or flood.remains > 0
        free_note = " (this exact query is free regardless of quota)" if getattr(flood, "query_is_free", False) else ""
        print(f"Search quota: {flood.remains}/{flood.total_daily} free daily full-text searches remaining{free_note}.")
        if not is_free:
            print(f"This exact search would cost {flood.stars_amount} Telegram Stars (free quota exhausted).")
            if getattr(flood, "wait_till", None):
                print(f"Free quota resets at unixtime {flood.wait_till}.")
            stars_needed = flood.stars_amount
        print()

    allow_paid_stars = None
    paid_stars_spent = None
    if stars_needed:
        if pay_stars_cap is None:
            await unavailable_then_maybe_fallback(
                [
                    "channels.searchPosts UNAVAILABLE for this account without payment.",
                    f"Reason: this query requires {stars_needed} Telegram Stars (free daily quota exhausted) and",
                    "no payment was authorized. Pass --pay-stars <N> (N >= the amount above) to authorize spending",
                    "real Telegram Stars for a genuine global public-post search. Nothing is ever paid automatically.",
                ]
            )
            return
        if pay_stars_cap < stars_needed:
            await unavailable_then_maybe_fallback(
                [
                    "channels.searchPosts UNAVAILABLE for this account at the authorized payment cap.",
                    f"Reason: this query requires {stars_needed} Telegram Stars, but --pay-stars only authorized",
                    f"{pay_stars_cap}. Refusing to spend more than authorized - raise --pay-stars to at least",
                    f"{stars_needed} to run a genuine paid global search.",
                ]
            )
            return
        logger.info("search strategy: AUTHORIZING PAYMENT of %d Telegram Stars for channels.searchPosts", stars_needed)
        print(f"Authorizing payment of {stars_needed} Telegram Stars (cap was {pay_stars_cap}) for a genuine global public-post search.")
        print()
        allow_paid_stars = stars_needed
        paid_stars_spent = stars_needed
    else:
        logger.info("search strategy: attempting channels.searchPosts (true public post search, free), q=%r", keyword)

    try:
        result = await call_search_posts(client, keyword, allow_paid_stars=allow_paid_stars)
    except PremiumAccountRequiredError:
        await unavailable_then_maybe_fallback(
            [
                "channels.searchPosts UNAVAILABLE for this account.",
                "Reason: Telegram requires Premium, remaining free quota, or an authorized Stars payment to call",
                "channels.searchPosts (RPC error: PREMIUM_ACCOUNT_REQUIRED). This is a genuine Telegram-side",
                "restriction on this specific method, not a bug in this POC - see README.",
            ]
        )
        return
    except FloodWaitError as exc:
        print("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT")
        print(f"Reason: FloodWaitError - Telegram is rate-limiting this account. Wait {exc.seconds}s before retrying.")
        return
    except (ConnectionError, OSError) as exc:
        print("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT")
        print(f"Reason: network/connection error during search ({exc}).")
        return
    except RPCError as exc:
        print("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT")
        print(f"Reason: Telegram RPC error ({exc.__class__.__name__}): {exc}")
        return

    chats_by_id, users_by_id = build_entity_index(result.chats, result.users)
    serialized = sort_latest_first([serialize_global_message(m, chats_by_id, users_by_id) for m in result.messages])

    output_path = build_output_path("public-search", keyword)
    write_output(serialized, output_path)
    print_search_results(keyword, serialized, output_path)

    if serialized:
        paid_note = f" (paid {paid_stars_spent} Telegram Stars for this search)" if paid_stars_spent else ""
        print(f"RESULT: PUBLIC POST SEARCH CONTENT RECEIVED{paid_note}")
    else:
        print("RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT")
        print(f"Possible reasons: no public channel post currently matches '{keyword}', or the match set is")
        print("naturally empty. No bypass attempted.")


# --- Entry point ---------------------------------------------------------------

async def run(args):
    api_id, api_hash = load_credentials()

    print("=" * 60)
    print("Telegram MTProto Scraping POC (Telethon)")
    print("=" * 60)

    channels = None
    if args.global_search:
        print(f"Mode: GLOBAL SEARCH via messages.searchGlobal, keyword '{args.global_search}'")
    elif args.public_search:
        fallback_note = " (auto-fallback to messages.searchGlobal disabled: --no-fallback)" if args.no_fallback else \
            " (auto-fallback to messages.searchGlobal if unavailable)"
        pay_note = f" (up to {args.pay_stars} Telegram Stars authorized if payment is required)" if args.pay_stars else \
            " (no Telegram Stars payment authorized)"
        print(f"Mode: PUBLIC POST SEARCH via channels.searchPosts, keyword '{args.public_search}'{fallback_note}{pay_note}")
    else:
        channels = args.channels or [get_default_channel()]
        print(f"Configured source(s): {', '.join(channels)}")
        if args.search:
            print(f"Mode: keyword search for '{args.search}' (up to {MESSAGE_LIMIT} matches per channel)")
        else:
            print(f"Mode: latest {MESSAGE_LIMIT} messages per channel (no keyword filter)")
    print("api_hash: [HIDDEN]")
    print()

    client = TelegramClient(SESSION_NAME, api_id, api_hash)

    if not await connect_and_authenticate(client):
        return

    if args.global_search:
        await do_global_search(client, args.global_search)
    elif args.public_search:
        await do_public_search(
            client, args.public_search, allow_fallback=not args.no_fallback, pay_stars_cap=args.pay_stars
        )
    else:
        await run_channel_mode(client, channels, args.search)

    await client.disconnect()


def main():
    # Message text/titles from Telegram can contain any Unicode (emoji, non-Latin
    # scripts, etc.) which crashes printing on Windows' default cp1252 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    args = parse_args()

    if (args.global_search or args.public_search) and (args.channels or args.search):
        print("ERROR: --global-search / --public-search cannot be combined with --channels or --search.")
        print("They call Telegram's own global search RPCs directly and ignore per-channel configuration.")
        sys.exit(1)

    if args.no_fallback and not args.public_search:
        print("ERROR: --no-fallback only applies to --public-search.")
        sys.exit(1)

    if args.pay_stars is not None:
        if not args.public_search:
            print("ERROR: --pay-stars only applies to --public-search.")
            sys.exit(1)
        if args.pay_stars <= 0:
            print("ERROR: --pay-stars must be a positive number of Telegram Stars.")
            sys.exit(1)

    args.channels = parse_channels_arg(args.channels)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nAborted by user.")
    except Exception as exc:
        # Last-resort guard so an unexpected failure doesn't dump a raw
        # traceback that could incidentally include sensitive details.
        print(f"UNEXPECTED ERROR: {exc.__class__.__name__}: {exc}")
        print("RESULT: NO CONTENT RECEIVED")


if __name__ == "__main__":
    main()
