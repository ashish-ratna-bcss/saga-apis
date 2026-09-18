# Telegram Scraper POC — Technical Report

**Prepared for:** Team Lead
**Reviewed:** 2026-09-04
**Subject:** `main.py` (602 lines, single file), the archived proof-of-concept now at `poc/`
**Method:** Static read of code, README, config, and captured run output — no live account inspection performed

---

## Executive Summary

The proof-of-concept (now archived at `poc/`) is a single-file Python/Telethon script that proves Telegram MTProto credentials work and that content can be pulled from **public** channels and groups. It exposes three genuinely different retrieval mechanisms — a per-channel fetch/search, a global keyword search, and a Premium-gated public-post search — and every one of them is capped at a single call of up to 100 results, with no pagination, no proxy/anti-ban handling, and no bypass of Telegram's own rate limiting.

The codebase is small and consistent with its own stated scope (its docstring calls it a "standalone connectivity/content-retrieval test only"). This review verifies that claim line by line, and separately flags one place where the README no longer matches what the code actually does, and one open question — whether global-search results include channels the account has *not* joined — that can't be settled by reading code alone.

| Metric | Value |
|---|---|
| Distinct retrieval mechanisms (RPC-backed) | 3 |
| Messages / results per call — hard cap | 100 |
| Channels per run — hard cap | 10 |
| Pagination loops implemented | 0 |

---

## Current Implementation

### Purpose and functional surface

Per its own docstring (`main.py:1-12`), the script exists to verify connectivity and content retrieval only: public channels/posts, no proxy rotation, no anti-ban or CAPTCHA handling, no crawling of channels the operator hasn't named, and — its own words — "not connected to any other project." The README frames it explicitly as a precursor: *"a separate integration design is expected as a follow-up once this POC confirms retrieval works."*

Runtime dependencies are minimal — `requirements.txt` pins exactly two packages: `telethon>=1.36.0` (the MTProto client) and `python-dotenv>=1.0.1` (loads `.env`). There is no web framework, database, or scheduler anywhere in the tree.

Five invocation shapes are supported today, each ending in a timestamped JSON file under `output/` and a console line beginning `RESULT:`: default fetch, `--search`, `--channels`, `--global-search`, and `--public-search` (detailed below).

### Authentication and session state

First-run login is interactive — phone number, verification code, and 2FA password if enabled — handled by Telethon's `client.start()` (`main.py:169-219`). The resulting session is persisted to `telegram_poc_session.session`, an on-disk SQLite file. **That file already exists in the working tree**, meaning this POC has completed a live login against a real Telegram account at least once; every subsequent run skips the interactive step and reconnects directly.

### Configuration state, as currently checked

`.env` currently has `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` populated, and `TELEGRAM_CHANNEL` left **blank**. `get_default_channel()` (`main.py:84-90`) hard-exits with an error if that variable is empty and no `--channels` value was passed — so a bare `python main.py` will not run successfully until either `.env` is filled in or `--channels` is supplied on every invocation.

---

## API / Endpoint Details

Two of the four calls below are raw MTProto requests the script builds and sends itself. The other two are Telethon convenience methods — the script never names the underlying wire method, so those mappings reflect Telethon's own documented client behavior, not something asserted purely from reading `main.py`.

| Call in code | MTProto method(s) | Invocation | Triggered by |
|---|---|---|---|
| `client.get_entity(channel)` | `contacts.resolveUsername` / `channels.getChannels` | Telethon-wrapped *(inferred from Telethon, not directly in code)* | default, `--search`, `--channels` |
| `client.get_messages(entity, limit=100, search=…)` | `messages.getHistory` (no keyword) / `messages.search` (with `--search`) | Telethon-wrapped *(inferred from Telethon, not directly in code)* | default, `--search`, `--channels` |
| `SearchGlobalRequest(…)` | `messages.searchGlobal` | Direct RPC — built at `main.py:442-454` | `--global-search` |
| `SearchPostsRequest(…)` | `channels.searchPosts` | Direct RPC — built at `main.py:489-498` | `--public-search` |

No other `telethon.tl.functions.*` request objects are imported anywhere in `main.py` — this table is the complete set. Entity/message serialization (`serialize_message`, `serialize_global_message`) is local formatting, not a network call.

---

## Search Modes

Legend used below: ✅ works on any account · ⛔ requires Telegram Premium · ⚠ deliberate bound in this code · ℹ Telegram-side fact, not this code's doing.

| Flag | Endpoint | Scope | Requires |
|---|---|---|---|
| *(none — default)* | `messages.getHistory` | Latest 100 messages from `TELEGRAM_CHANNEL` or `--channels`, no keyword | ✅ any account |
| `--search KEYWORD` | `messages.search` | Same configured channel(s) only — up to 100 matches each, server-side search | ✅ any account |
| `--channels A,B,C` | *(modifier, not its own mode)* | Up to 10 explicit public channels/groups per run; combinable with `--search` | ✅ any account |
| `--global-search KEYWORD` | `messages.searchGlobal` | Public **broadcast** channels only (`broadcasts_only=True`), ranked by Telegram's own account-scoped global index | ✅ any account |
| `--public-search KEYWORD` | `channels.searchPosts` | Public channel posts — Telegram's curated cross-channel post-search feature | ⛔ Telegram Premium |

Mutual exclusivity is enforced twice: an `argparse` mutually-exclusive group covers `--global-search` / `--public-search` (`main.py:111-125`), and a second, manual guard in `main()` rejects either of those combined with `--channels` / `--search` (`main.py:582-585`).

### Global search: does it reach channels the account hasn't joined?

`SearchGlobalRequest` is called with `broadcasts_only=True` and no channel list of its own (`main.py:442-454`) — so it is not a loop over `--channels`, and it does reach beyond a single configured chat. That much is verifiable directly in the code and confirmed by a captured run: results came back tagged with a channel identity distinct from any explicitly configured source.

> **Open question — not answerable from code alone.** `serialize_global_message()` (`main.py:401-418`) records `channel_title`, `channel_username` and `sender_id` for each hit, but **no field records whether the authenticated account is a member of / subscribed to that source channel.** The JSON output as structured today cannot distinguish "channels I follow" from "channels Telegram's index surfaced." Confirming that would require cross-referencing returned channel usernames against a live call to `client.get_dialogs()` at runtime — not implemented here, and not tested in this review.
>
> The README asserts (its own claim, not independently verified against Telegram's server-side behavior in this review) that results are "ranked by Telegram's own global index for this account," and that no Telegram method full-text-searches every public channel regardless of membership. Treat that as documentation of intent, not a verified guarantee.

### Keyword search flow — traced end to end, with recent evidence

Call path for the default / `--search` / `--channels` modes:

1. `parse_args()` → `load_credentials()` reads `.env`
2. `connect_and_authenticate()` — connects, reuses the session file if present
3. Per channel: `fetch_channel()` → `client.get_entity(channel)` → `client.get_messages(entity, limit=100, search=keyword)`
4. `serialize_message()` per message, results concatenated across all requested channels
5. `build_output_path()` names a fresh file; `write_output()` writes it
6. Console summary printed, ending in a `RESULT:` line

The working tree's `output/` folder holds direct evidence of this flow being exercised recently — five `--global-search` runs inside a 14-minute window on 2026-09-03. Record counts (not message content) are reproduced below:

| Output file | Mode / keyword | Records | Note |
|---|---|---|---|
| `messages.json` | Channel-mode run (22:09:50) | 10 | Pre-dates current filename convention — see Limitations |
| `global-search_drug_…221803.json` | `--global-search "drug"` | 100 | Hit the cap |
| `global-search_hot_…222100.json` | `--global-search "hot"` | 100 | Hit the cap |
| `global-search_knife_…222235.json` | `--global-search "knife"` | 100 | Hit the cap |
| `global-search_murder_…223153.json` | `--global-search "murder"` | 10 | Below cap — genuine result-set size |
| `global-search_rape_…223135.json` | `--global-search "rape"` | 0 | Zero matches — 2-byte `[]` file |

This one sequence demonstrates all three outcomes the code can produce from a single call: capped at 100, a genuine partial result set, and an empty set — none of which trigger a retry or a second page.

---

## Limits & Constraints

| Constraint | Value | Enforced at |
|---|---|---|
| Messages / results per call | 100 | `MESSAGE_LIMIT`, `main.py:54` |
| Channels per run | 10 | `MAX_CHANNELS_PER_RUN`, `main.py:55` → `parse_channels_arg()`, `129-137` |
| Pagination loops | 0 | see below |

**Pagination:** every RPC call fires exactly once per run. `SearchGlobalRequest` and `SearchPostsRequest` both accept `offset_rate` / `offset_peer` / `offset_id` — the parameters Telegram uses for paging — but the code always passes the zero/empty starting values, and neither `do_global_search()` nor `do_public_search()` loops or re-issues the request. The cursor plumbing exists; nothing drives it.

**Premium requirement:** `--public-search` (`channels.searchPosts`) requires Telegram Premium on the account behind the configured credentials. The code catches `PremiumAccountRequiredError` explicitly (`main.py:499-503`) and reports it rather than attempting a workaround. Every other mode works on a standard account.

**Rate limiting:** `FloodWaitError` is caught at four separate call sites — channel resolution, message fetch, global search, public search — each reporting the required wait time. There is no sleep-and-retry loop and no credential rotation anywhere in the file.

**Confirmed absent:** no proxy configuration, no device/user-agent spoofing beyond Telethon's defaults, no CAPTCHA handling, no crawling logic that discovers channels beyond what the operator names or what Telegram's own search ranks.

---

## Data Flow

This is a synchronous, single-run CLI script. `asyncio` is used only to drive the Telethon client — there is no server, no queue, no database. The only persistence is one JSON file per run on local disk.

```
CLI flags + .env (API_ID / API_HASH / CHANNEL)
        │
        ▼
connect_and_authenticate()  — reuses session file if present
        │
        ├──────────────┬──────────────────┐   routed by CLI flag
        ▼              ▼                  ▼
 get_entity +    SearchGlobalRequest   SearchPostsRequest
 get_messages    messages.searchGlobal channels.searchPosts
 (Telethon-      (direct RPC,          (direct RPC,
  wrapped)        broadcasts_only)      requires Premium)
 default/         --global-search       --public-search
 --search/
 --channels
        │              │                  │
        └──────────────┴──────────────────┘
                        │   limit = 100, offset params present
                        │   but never advanced → one page, no pagination
                        ▼
        serialize_message() / serialize_global_message()
                        │
                        ▼
        write_output() — timestamped JSON → output/
                        │
                        ▼
        console summary, ending in a RESULT: line
```

Solid-path RPCs (`SearchGlobalRequest`, `SearchPostsRequest`) are raw MTProto requests built directly in `main.py`; the `get_entity`/`get_messages` path is Telethon's own high-level wrapper. All three paths converge on one serializer, one JSON writer, and one console summary — and none of them loop back for a second page.

### Files and responsibilities

| File | Responsibility |
|---|---|
| `main.py` | Entire application — CLI parsing, auth, all three retrieval mechanisms, serialization, JSON output, console reporting (~600 lines, single file) |
| `README.md` | Setup steps, flag reference, the three-mode comparison table, stated scope/limitations |
| `requirements.txt` | Two dependencies: `telethon`, `python-dotenv` |
| `.env` | Runtime credentials — `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_CHANNEL` — gitignored |
| `.gitignore` | Excludes `.env`, `*.session*`, `output/*.json`, `__pycache__`, `.venv` |
| `telegram_poc_session.session` | Telethon's persisted MTProto session (SQLite) — present, so login has already completed once |
| `output/*.json` | One file per run, named `mode[_keyword]_timestamp.json`; holds serialized results |

---

## Limitations

### What the code does

- Retrieves messages from named public channels/groups, latest or keyword-filtered, via server-side search
- Runs three distinct RPC-backed retrieval mechanisms (see API/Endpoint Details)
- Checks up to 10 explicit channels in one run
- Writes structured, timestamped JSON per run
- Reports every terminal state honestly via an explicit `RESULT:` line — including empty results, rather than masking them
- Keeps `api_hash` out of every log/print statement, and gitignores secrets and session files

### What the code does not do

- No access to private chats — `ChannelPrivateError` is caught and reported, never bypassed
- No pagination or bulk historical crawl — one page, one call, per run
- No continuous or scheduled scraping — this is a manually invoked CLI
- No proxy rotation, anti-ban technique, or CAPTCHA handling
- No rate-limit bypass — `FloodWaitError` stops the run and reports the wait time
- No subscription/membership tag on global-search results (see Search Modes)
- No de-duplication or merge across runs — each output file stands alone
- No integration with any other system — confirmed standalone, per its own docstring

### Known Telegram-side constraints (external to this code)

These are properties of Telegram's API itself, reflected in the README's own framing rather than enforced by anything this script does: `messages.searchGlobal` has no mode that full-text-searches every public channel regardless of membership — it returns whatever Telegram's server-side ranking decides for the calling account; `channels.searchPosts` is gated to Premium accounts entirely server-side; `FloodWaitError` is Telegram's own throttling response, not a client-side limitation.

### Documentation drift found during this review

**README §G says:** "All modes write to the same `output/messages.json` (overwriting it)," and its sample output shows `Output: output/messages.json`.

**The code does not do this.** `build_output_path()` (`main.py:153-160`) always generates a unique `mode[_keyword]_timestamp.json` name, and the literal string `"messages.json"` does not appear anywhere in `main.py` — confirmed by direct search of the file. An `output/messages.json` does exist on disk (written 2026-09-03 22:09:50), but its timestamp predates that run's sibling timestamped files and it could not have been produced by the script as it stands today. It's a leftover from an earlier version, not current behavior. **The README should be updated.**

---

## Recommendations

| # | Recommendation | Effort |
|---|---|---|
| 1 | Correct README §G/§H to describe the actual timestamped-filename output behavior instead of a single `messages.json`. | Quick, docs-only |
| 2 | Decide whether global-search results need a subscription/membership tag per source channel before this data feeds anything downstream — requires cross-referencing `client.get_dialogs()` against result peers at runtime. | Needs a decision |
| 3 | If broader coverage becomes a goal, pagination is the natural next increment — `offset_rate`/`offset_peer`/`offset_id` are already threaded through both search RPCs; the gap is a loop and a stop condition, not new API surface. | Medium |
| 4 | Fill in `TELEGRAM_CHANNEL` or formally standardize on always passing `--channels` — default-mode runs currently hard-exit on the blank value. | Quick config fix |
| 5 | Before any integration beyond this POC, define retention/access rules for `output/*.json` — it now holds real third-party message content, gitignored but unencrypted with no expiry on local disk. | Needs a decision |
| 6 | If more RPC calls get added later, centralize the `FloodWaitError` handling currently duplicated across four call sites. Not urgent at the current size. | Low priority |

---

## Conclusion

The POC does what it claims: it proves MTProto credentials work and that public content is retrievable through three distinct, real Telegram RPC mechanisms, staying inside deliberate bounds — 100 results per call, 10 channels per run, no pagination, no bypass techniques of any kind. It is a feasibility check, not a scraping pipeline: there's no persistence layer beyond flat JSON files and no scheduling.

Two things are worth resolving before this goes further: aligning the README with the output-file behavior the code actually has, and deciding what a "global search" result is allowed to mean with respect to channel membership before that field gets treated as reliable. Neither is a large change; both are decisions, not just cleanup.

---

*Scope of this review: `main.py`, `README.md`, `requirements.txt`, `.gitignore`, `.env` (structure only — credential values withheld from this report), file presence/timestamps under `output/` and the session file. No live Telegram account behavior was queried as part of this review.*
