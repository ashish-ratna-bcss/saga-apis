# Proof-of-concept CLI (archived)

> Archived. This standalone script has been superseded by the service in
> `app/` and now lives at `poc/`. It is kept because it is the reference the
> service's Telegram layer was built from, and it remains runnable.


Standalone proof-of-concept: verify that Telegram MTProto API credentials work
and that content can be retrieved from a **public** Telegram channel/group
using [Telethon](https://docs.telethon.dev/).

This is a connectivity/content-retrieval test only. It is **not** connected to
any other project, does not scrape private chats, and does not implement any
proxy rotation, anti-ban, CAPTCHA bypass, or rate-limit bypass techniques.

## A. Create the Python environment

From inside `poc/`:

```bash
python -m venv .venv
```

Activate it:

- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Windows (cmd.exe): `.venv\Scripts\activate.bat`
- macOS/Linux: `source .venv/bin/activate`

## B. Install dependencies

```bash
pip install -r requirements.txt
```

This installs `telethon` (MTProto client) and `python-dotenv` (loads `.env`).

## C. Create `.env`

A `.env` file already exists in this folder as a template. Open it and fill
in your real values — it is listed in `.gitignore` and will not be committed.

```
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_CHANNEL=@example_channel
```

Get `api_id` / `api_hash` from https://my.telegram.org/apps (an existing
app's credentials, e.g. your SOCEYE app registration, will work fine — this
POC only reads them from the environment, never hardcodes them).

## D. Where to put TELEGRAM_API_ID

In `.env`, as `TELEGRAM_API_ID=<numeric id>` (numbers only, no quotes).

## E. Where to put TELEGRAM_API_HASH

In `.env`, as `TELEGRAM_API_HASH=<hash string>`. This value is loaded at
runtime only — the script never prints, logs, or writes it anywhere.

## F. How to specify a public Telegram channel

In `.env`, set `TELEGRAM_CHANNEL` to a public channel/group username or link,
e.g.:

```
TELEGRAM_CHANNEL=@durov
```

or

```
TELEGRAM_CHANNEL=https://t.me/durov
```

Only public channels/groups are supported. Private chats will be reported as
inaccessible by design (see item 12 in the project requirements).

`TELEGRAM_CHANNEL` is **not** required for `--global-search` or
`--public-search` (see section G) — those modes search across Telegram's own
global index instead of one configured channel, so this variable is simply
ignored for them.

## G. How to run the program

Latest 100 messages from the configured channel (default mode):

```bash
python main.py
```

Keyword search within that same channel — returns up to 100 messages that
contain the given keyword, using Telegram's own server-side search (still
scoped to the single public channel in `TELEGRAM_CHANNEL`, not a multi-chat
search):

```bash
python main.py --search "keyword"
```

Check up to 10 explicit public channels/groups in one run (optionally
combined with `--search`):

```bash
python main.py --channels "durov,telegram"
python main.py --channels "durov,telegram" --search "keyword"
```

True Telegram-wide search via `messages.searchGlobal` (see "Search modes
explained" below for what this actually covers):

```bash
python main.py --global-search "narcotics"
```

True global public-post search via `channels.searchPosts` — this genuinely
covers public channels this account has **not joined**, not just its own
subscriptions (see "Search modes explained" below). Every account gets a
small free daily quota for this; the script checks it automatically
(`channels.checkSearchPostsFlood`, free) before searching:

```bash
python main.py --public-search "narcotics"
```

If the free quota is exhausted, the command above reports exactly how many
Telegram Stars the search would cost and falls back to `messages.searchGlobal`
without spending anything. To actually pay and get the true global result
instead of the fallback, authorize a Stars amount explicitly:

```bash
python main.py --public-search "narcotics" --pay-stars 50
```

This only pays if Telegram's quoted price is `<= 50` — otherwise it refuses
and reports the real price. **This spends real Telegram Stars (real money)
— it is never triggered without `--pay-stars` on that specific run.**

Same call, but without the automatic fallback (fails plainly instead) —
useful if you specifically want to confirm whether `channels.searchPosts`
itself is available/affordable on this account:

```bash
python main.py --public-search "narcotics" --no-fallback
```

`--global-search` and `--public-search` are mutually exclusive with each
other and with `--channels`/`--search` — they call Telegram's own
global-scope search RPCs directly and ignore `TELEGRAM_CHANNEL`, `--channels`,
and `--search` entirely (`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are still
required, since every mode needs an authenticated MTProto session).
`--no-fallback` and `--pay-stars` are only valid together with `--public-search`.

All modes write their results to their own timestamped file under `output/`
and print a `RESULT:` line summarizing the outcome.

### Search modes explained

This POC has three genuinely different **MTProto methods** it can call for
searching. `--public-search` can invoke two of them in a single run (the real
method, then the fallback, only if needed):

| Mode | Flag | MTProto method | What it actually searches | Requires |
|---|---|---|---|---|
| Channel search | `--search` (+ `--channels`/`TELEGRAM_CHANNEL`) | `messages.search` (via Telethon's `get_messages(..., search=...)`) | Only the specific channel(s)/group(s) you name — up to 10 per run. Public channels do **not** need to be joined first; you just need to know the username. | Any account |
| Global search | `--global-search` | `messages.searchGlobal` | Public **broadcast channels** matching the keyword, as ranked by Telegram's own global index for this account (this script sets `broadcasts_only=True`) — in practice this is scoped to channels/chats this account already has visibility into (usually ones it has joined), **not** every public channel on Telegram | Any account, free |
| Public post search (true method) | `--public-search` | `channels.searchPosts` | Public channel posts matching the keyword — Telegram's own docs say this explicitly **"includ[es] those we aren't a member of."** This is the only one of the three that is a genuine, unrestricted-by-membership **global public-post search**. | A free daily quota (any account), Telegram Premium, or paying Telegram Stars per search beyond that quota — see below. Not simply "Premium-only." |
| Public post search (fallback) | `--public-search` (automatic, only when the true method above is unaffordable/unavailable) | `messages.searchGlobal` (same method as Global search, `broadcasts_only=True`) | The same account-scoped index as `--global-search` — **not** the same corpus as `channels.searchPosts` — plus a local, literal, case-insensitive re-check of each returned message's text, since Telegram's global index can return token/fuzzy matches | Any account, free |

**Important — a correction from an earlier version of this README:**
`channels.searchPosts` is **not** gated to Telegram Premium accounts only.
Telegram's own API documentation
(`core.telegram.org/method/channels.searchPosts`) states: *"each user has a
limited amount of free full text search slots, after which payment is
required."* So a plain, non-Premium account genuinely can search public
channels it has never joined — for free, up to a daily quota, then by paying
Telegram Stars per extra search. The `PREMIUM_ACCOUNT_REQUIRED` RPC error
this script may report means the account's free quota is exhausted *and* no
Stars payment was authorized for that call — not that the feature is
permanently locked behind a Premium subscription. `messages.searchGlobal`
(used by `--global-search` and the fallback) has no such daily quota, but it
is scoped to what Telegram's index already associates with the account —
there is still no Telegram API that free-text-searches *every* public
channel in existence with no quota and no membership signal at all.

### The free quota + Telegram Stars payment model (`--public-search`)

Before attempting `channels.searchPosts`, the script always calls the free,
read-only `channels.checkSearchPostsFlood` to find out where this account
stands, and prints it plainly:

```
Search quota: 0/5 free daily full-text searches remaining.
This exact search would cost 25 Telegram Stars (free quota exhausted).
```

What happens next depends on that result and on `--pay-stars`:

1. **Free quota available, or this exact query is flagged free** — the real
   `channels.searchPosts` search runs immediately, no payment involved.
2. **Quota exhausted, `--pay-stars` not given** — nothing is paid. The script
   reports the exact Stars price Telegram quoted, then (unless
   `--no-fallback`) falls back to `messages.searchGlobal`.
3. **Quota exhausted, `--pay-stars N` given and `N` covers the quoted
   price** — the script authorizes paying *exactly* the quoted amount (never
   more than `N`, and never more than Telegram's own quote) and runs the true
   global search. Real Telegram Stars are spent on your account for this.
4. **Quota exhausted, `--pay-stars N` given but `N` is below the quoted
   price** — the script refuses to pay and reports the shortfall, then falls
   back (unless `--no-fallback`).
5. **The free quota check itself fails or is rejected** (e.g. bot accounts —
   Telegram restricts this check to user accounts — or a transient RPC/network
   error) — the script logs that and attempts `channels.searchPosts` directly
   with no payment authorized, exactly as if quota information weren't
   available, so it still works the same as before this quota-check existed.

**This script never spends Telegram Stars unless you pass `--pay-stars N` on
that specific command — there is no default amount, no auto top-up, and no
persistent authorization across runs.** A successful paid search's `RESULT:`
line always says so explicitly, e.g.
`RESULT: PUBLIC POST SEARCH CONTENT RECEIVED (paid 25 Telegram Stars for this search)`.

### The `messages.searchGlobal` fallback, and why it looks like "only my channels"

When the true method above isn't run (quota exhausted and no/insufficient
payment, or Telegram otherwise rejects it outright), the script falls back to
calling `messages.searchGlobal` with `broadcasts_only=True` — a different,
always-free MTProto method:

1. Prints a plain, explicit statement of why the true method didn't run.
2. Calls `messages.searchGlobal` (the same method `--global-search` uses).
3. Locally re-verifies each message Telegram returns actually contains the
   literal keyword (case-insensitive substring), since that index is
   token/fuzzy-based and can otherwise return near-matches.
4. Writes results to `output/public-search-fallback_<keyword>_<timestamp>.json`
   and prints its own `RESULT: FALLBACK SEARCH ...` line — **never** the
   `RESULT: PUBLIC POST SEARCH ...` line that only a genuine
   `channels.searchPosts` success produces. The two are never conflated.

**What the fallback can search:** public **broadcast channels** that
Telegram's own global search index surfaces for the *authenticated account*,
i.e. the same scope as `--global-search`.

**What the fallback cannot search:**
- It is **not** a true Telegram-wide public-post search, and does **not**
  reach channels this account has no visibility into the way the paid/free
  `channels.searchPosts` does — in practice, results tend to be concentrated
  in channels this account has already joined/can already see. If your
  fallback results keep coming from the same small set of channels, that is
  this Telegram-side scoping at work, not a bug in this script. The fallback
  output prints a `Drawn from N distinct channel(s): ...` line so this is
  visible at a glance.
- It cannot see public **groups** (only broadcast channels), private
  chats/channels, or channels this account/Telegram's index has no visibility
  into.
- It is still capped at one call for up to 100 results — no pagination.

Results from every search mode (`--global-search`, `--public-search`, and its
fallback) are sorted most-recent-first client-side after Telegram returns
them, so "the latest keyword matches" is a reliable reading of the top of the
output regardless of Telegram's internal relevance ranking. `--search`/
`--channels` remain the right tool when you already know which channel(s) to
check and want their full history for free, with no quota at all.

## H. What successful output looks like

On the **first run**, Telethon will prompt interactively in the terminal for:

1. Your phone number (international format, e.g. `+15551234567`)
2. The verification code Telegram sends you
3. Your 2FA password, only if your account has one enabled

After that, a summary like this is printed:

```
Telegram connection: SUCCESS
Authentication: SUCCESS
Source: @example_channel
Source type: PUBLIC_CHANNEL
Messages requested: 100
Messages received: 87
Messages containing text: 72
Messages containing media: 15
Output: output/fetch_20260904_101500.json

RESULT: CONTENT RECEIVED SUCCESSFULLY
```

If no messages come back, the script reports:

```
RESULT: NO CONTENT RECEIVED
```

along with the likely reason (empty channel, no access, invalid channel,
rate limiting, network issue, etc.) instead of pretending it worked.

`--global-search` and `--public-search` print their own analogous result
lines instead, so it's clear which mode/method actually produced the outcome:

```
RESULT: GLOBAL SEARCH CONTENT RECEIVED
RESULT: GLOBAL SEARCH RETURNED NO CONTENT

RESULT: PUBLIC POST SEARCH CONTENT RECEIVED                              (genuine channels.searchPosts success, free or paid)
RESULT: PUBLIC POST SEARCH RETURNED NO CONTENT

RESULT: FALLBACK SEARCH CONTENT RECEIVED                                 (messages.searchGlobal fallback)
RESULT: FALLBACK SEARCH RETURNED NO CONTENT
```

All three search modes also print, per matching message: source/channel,
message ID, date, a text preview, and whether media is present — before the
`RESULT:` line. Example of `python main.py --public-search "attack"` with the
free daily quota exhausted and no `--pay-stars` given:

```
Public post search keyword: 'attack'
Method: channels.searchPosts (Telegram MTProto) - true Telegram-wide public post search
(genuinely covers public channels this account has not joined - see README)

Search quota: 0/5 free daily full-text searches remaining.
This exact search would cost 25 Telegram Stars (free quota exhausted).
Free quota resets at unixtime 1999999999.

channels.searchPosts UNAVAILABLE for this account without payment.
Reason: this query requires 25 Telegram Stars (free daily quota exhausted) and
no payment was authorized. Pass --pay-stars <N> (N >= the amount above) to authorize spending
real Telegram Stars for a genuine global public-post search. Nothing is ever paid automatically.

Falling back to: messages.searchGlobal (Telegram MTProto)
This fallback searches Telegram's own public-broadcast-channel message index for this account.
It is NOT channels.searchPosts and is not claimed to be an equivalent global public-post search.

Keyword: 'attack'
Results: 3
- source=@example_news message_id=451 date=2026-09-01T10:22:00+00:00 media_present=False text='...an attack was reported near...'
...
Output: output/public-search-fallback_attack_20260904_101500.json

(Telegram's global index returned 4 candidate message(s) for this account; 3 contain the literal keyword 'attack' after local re-verification. Sorted most-recent-first.)
Drawn from 2 distinct channel(s): @example_news, @another_channel

RESULT: FALLBACK SEARCH CONTENT RECEIVED
```

Running the same command with `--pay-stars 50` instead skips the fallback
entirely and ends with:

```
Authorizing payment of 25 Telegram Stars (cap was 50) for a genuine global public-post search.

Keyword: 'attack'
Results: 12
...
Output: output/public-search_attack_20260904_101500.json

RESULT: PUBLIC POST SEARCH CONTENT RECEIVED (paid 25 Telegram Stars for this search)
```

Log lines showing which search strategy ran (e.g. "attempting
channels.searchPosts", "falling back to messages.searchGlobal") are written
to **stderr** via Python's `logging` module, separate from the stdout output
above, so they don't interfere with anything scripting against stdout.

## I. Where the retrieved messages are stored

Each run writes its own timestamped JSON array under `output/`, named
`<mode>[_<keyword>]_<YYYYMMDD_HHMMSS>.json`, so successive runs never
overwrite each other's results:

| Mode | Filename prefix |
|---|---|
| Default fetch (no `--search`) | `fetch_<timestamp>.json` |
| `--search` / `--channels` + `--search` | `search_<keyword>_<timestamp>.json` |
| `--global-search` | `global-search_<keyword>_<timestamp>.json` |
| `--public-search` (true `channels.searchPosts` success) | `public-search_<keyword>_<timestamp>.json` |
| `--public-search` (fallback via `messages.searchGlobal`) | `public-search-fallback_<keyword>_<timestamp>.json` |

Each entry is one object per message, with fields: `message_id`,
`channel_title`, `channel_username`, `text`, `date`, `sender_id`,
`reply_to_message_id`, `media_present`, `message_url`. For the
`--public-search` fallback specifically, the file contains only the messages
that passed local keyword re-verification (see "The messages.searchGlobal
fallback" above) — not every candidate Telegram's index returned.

All of these are excluded from git via `.gitignore` (`output/*.json`) since
they contain real message content.

## J. How the Telegram session works

The first successful login creates a local session file,
`telegram_poc_session.session`, in this folder (a SQLite database managed by
Telethon holding the authorized MTProto session key). On every subsequent
run, the script detects this file, skips the interactive login entirely, and
connects directly.

Delete `telegram_poc_session.session` (and any `.session-journal` file next
to it) to force a fresh login next run.

## K. Security precautions for api_hash and session files

- `TELEGRAM_API_HASH` is only ever read from the environment (`.env`) at
  runtime. It is never printed to the terminal, written to logs, or included
  in any `output/*.json` file.
- `.env` is listed in `.gitignore` — never commit it, and never paste its
  contents into issues, chat, or screenshots.
- `*.session` / `*.session-journal` files are also gitignored. Anyone who
  obtains your `.session` file can act as your logged-in Telegram account
  without needing your password or 2FA code again — treat it like a
  credential and delete it when you're done testing.
- `output/*.json` is gitignored too, since it contains real retrieved
  message content from the tested channel/group.
- If you ever suspect `api_hash` or a session file has leaked, revoke active
  sessions from Telegram (Settings → Devices) and regenerate the app's
  `api_hash` at https://my.telegram.org/apps.

## Scope / limitations (intentional)

- Public channels/groups and public channel posts only — no private-chat
  access, in any mode.
- Fetches only up to 100 messages/results per call, once per run — no
  pagination loop, no continuous/large-scale scraping.
- `--channels` is capped at 10 channels per run — a deliberate limit so this
  stays a bounded test tool, not a crawler.
- `--global-search` sets `broadcasts_only=True` so results stay scoped to
  public channels rather than your private chats/groups.
- `--public-search`'s true method, `channels.searchPosts`, is genuinely
  usable by any account (Telegram's own docs: it covers channels "we aren't
  a member of"), but only up to a limited free daily quota, checked via
  `channels.checkSearchPostsFlood`; beyond that, Telegram requires either an
  authorized Telegram Stars payment (`--pay-stars`, real money) or Premium.
  This script never pays automatically — when the quota is exhausted and no
  sufficient `--pay-stars` was given, it automatically falls back to
  `messages.searchGlobal` (see "The messages.searchGlobal fallback") and
  reports that fallback under its own distinct `RESULT: FALLBACK SEARCH ...`
  label — it is never presented as equivalent to a genuine
  `channels.searchPosts` result.
- The `--public-search` fallback is bounded by whatever `messages.searchGlobal`
  itself is scoped to: public **broadcast channels** visible to Telegram's
  global index for the authenticated account (in practice, usually channels
  it has already joined). It cannot search public groups, and — unlike the
  paid/free `channels.searchPosts` — it cannot reach channels this account
  has no visibility into.
- No proxy rotation, anti-ban techniques, CAPTCHA bypass, or rate-limit
  bypass. On `FloodWaitError`, the script stops and reports it rather than
  waiting/retrying automatically.
- This POC is intentionally standalone and is not wired into any other
  project. A separate integration design is expected as a follow-up once
  this POC confirms retrieval works.

## L. Running the tests

Unit tests mock out `TelegramClient` entirely (no real Telegram connection,
no `.env` credentials needed, and **no real Telegram Stars are ever spent by
the test suite** — `channels.checkSearchPostsFlood` and `channels.searchPosts`
are both fully faked) and cover: the free-quota check, the
`--pay-stars` payment authorization/refusal logic, the Premium/quota
restriction on `channels.searchPosts`, the automatic fallback to
`messages.searchGlobal`, local keyword re-verification, result sorting,
zero-result cases, `FloodWaitError`/RPC/network error handling, and the
existing `--search`/`--channels`/`--global-search` modes. Run with either:

```bash
python -m unittest discover -s tests -v
```

or, if `pytest` is installed in your environment:

```bash
pytest tests/ -v
```
