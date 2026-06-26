# Gödel REST — token relay + pollers

Stream Gödel Terminal chat & news by polling `api.godelterminal.com` directly
with the Bearer JWT your logged-in browser already holds. No DOM scraping, no
Cloudflare fight (Cloudflare guards `app.`, not `api.`).

## How it works

```
  your normal Chrome (logged in)
        │  app makes api.godelterminal.com calls
        ▼
  extension/  (MAIN-world fetch/XHR hook)  ── sniffs Authorization: Bearer ──┐
                                                                              ▼
                                                       token_server (127.0.0.1:8765)
                                                                              │ writes
                                                                              ▼
                                                          ~/.godel_rest/token.json
                                                                              │ read fresh per call
                                       chat / news pollers ──► requests ──► api.godelterminal.com
                                                                              │
                                                                              ▼
                                                              godel_rest.db (SQLite)
```

The hook only **observes** requests (never modifies them). Every API call the
app makes re-feeds the freshest token, so token refresh is automatic as long as
the tab stays logged in.

## Setup

1. **Install dep:** `pip install -r requirements.txt` (only `requests`).
2. **Start the receiver** (leave running):
   ```bash
   cd godel_rest
   python3 -m server.token_server
   ```
3. **Load the extension** in Chrome:
   - `chrome://extensions` → enable *Developer mode* → *Load unpacked* → pick
     `godel_rest/extension/`.
4. **Log in** at https://app.godelterminal.com and click around (open any
   window — DES, chat, etc.) to trigger one API call. The extension popup should
   show *Delivered to local poller ✓* and the receiver prints the token expiry.

## Use

```bash
python3 -m server.cli status      # token expiry + db counts
python3 -m server.cli verify      # Task 1: confirm API returns 200 server-side
python3 -m server.cli channels    # list all 63 chat channels (id/title/type)
python3 -m server.cli chat --channels general,biotech,options,paid   # stream (forward)
python3 -m server.cli backfill --channels general --max-pages 5      # history (backward)
python3 -m server.cli backfill --channels all                       # full history, all rooms
python3 -m server.cli news        # stream top-news feed -> SQLite (30s poll)
python3 -m server.cli news --important
python3 -m server.cli trending    # ranked trending tickers by mentions (24H)
python3 -m server.cli discover    # api paths the extension saw the app call
python3 -m server.cli raw /api/v1/trending --param timeframe=24H     # poke any path
```

Chat options: `--channels <titles-or-uuids> --interval 3 --size 50`
(omit `--channels` to stream just the default `options` channel).
News options: `--important --interval 30`.

### Channels

63 channels total. The valuable community rooms are `type=user_write`:
`general, biotech, options, sellside, buyside, quant, smallcaps, crypto, fx,
bonds, energy, politics, …` plus `paid` (`user_only`) and `pikers`
(`public_write`). `symbol` channels are per-ticker rooms; `dm` are private.
Run `channels` for the full id/title map.

### Backfill (full history)

`backfill` walks a channel backward via `beforeCursor` until the first message,
saving every field (date, user, content, reactions, reply parent id/user/text,
ticker mentions). It is **resumable** (continues from the oldest id already
stored) and **idempotent** (INSERT OR REPLACE on int64 id — overlap never
duplicates). Safe to run in the background:

```bash
# full history of every community room (user_write/user_only/public/admin)
nohup python3 -m server.cli backfill --channels all --delay 0.3 \
      > backfill.log 2>&1 &
tail -f backfill.log
```

Options: `--channels all|<list>`, `--types <comma>` (filter when `all`),
`--max-pages N` (test cap), `--delay S` (politeness), `--no-resume`.

### Discovering new endpoints

The extension logs every distinct `api.` path the app calls to
`~/.godel_rest/endpoints.log`. To find an endpoint for a command not yet wired
up (e.g. RES, FA, breaking news): reload the extension, open that window in the
terminal, then `python3 -m server.cli discover`.

## Data

SQLite at `godel_rest/godel_rest.db`:

- `chat_messages` — one row per message (idempotent on int64 `id`).
- `chat_tickers` — expanded `[TICKER:seriesId]` mentions joined to
  `seriesContexts` (ticker, name, figi, isin, cik, series_type).
- `news_items` — normalized news (source, type, time, important, content,
  symbols), raw payload kept.

## Status / next

- [x] Extension token relay (MAIN-world hook) + local receiver + SQLite sink.
- [x] **Task 1 confirmed** — `verify` returns 200 server-side, no Cloudflare.
- [x] Chat poller (multi-channel, dedup, ticker tagging). Tested on 4 channels.
- [x] Channel enumeration via `/api/chat/channels` (63 channels mapped).
- [x] News poller — `/api/v1/top-news-items` (`?important=true` supported).
- [x] Trending — `/api/v1/trending?timeframe=24H` (ranked by mentions).
- [x] Endpoint discovery logger in the extension.
- [ ] Breaking news — old `/api/fetchBreaking` is 404. Find the replacement via
      `discover` (open the News/breaking window in the app).
- [ ] Cursor param for history backfill (response gives `prevCursor`; test
      `?cursor=`/`?before=`/`?prevCursor=` via `raw`).
- [ ] Confirm `createdAt` is UTC.
- [ ] More text commands (DES/FA/ANR/etc.) once their endpoints are discovered.

If `verify` ever returns a 403 Cloudflare challenge on the `api.` host (not
expected), the fallback is to run the fetch inside the page via the extension
instead of server-side. The MAIN-world hook is already the right place to add
that.

## Analytics + dashboard

Turns the chat archive into alpha signals. Core idea: every ticker mention is an
**event** `(user, ticker, time, direction)`; we score it against the **forward
market-adjusted return** of that ticker (prices from the Massive API). Skill =
does the signed call predict the move.

```
analytics/
  prices.py      Massive API daily bars -> prices cache (incremental)
  sentiment.py   lexicon direction classifier (swappable for FinBERT/LLM)
  events.py      chat_tickers+messages -> events table (direction labelled)
  returns.py     forward 1/5/20d returns, market-adjusted vs SPY (no look-ahead)
  skill.py       user leaderboard: hit rate, mean abn return, t-stat, SHRUNK
  signals.py     smart-money-weighted sentiment + attention/buzz spikes
  clustering.py  cluster users by what they trade (TF-IDF + SVD + KMeans)
  network.py     'mentioned-after' influence graph + PageRank
  userstats.py   activity stats (top senders, msgs/day, hour/weekday)
  pipeline.py    incremental refresh: events -> prices -> returns
```

### Run

```bash
# one incremental refresh (cheap to re-run as new chat arrives)
python3 -m analytics.run refresh

# better direction labels via LLM (understands trader slang the lexicon misses):
#   backends: lexicon (default, instant) | finbert (weak on slang) |
#             ollama (LLM, best) | hybrid (lexicon for clear, LLM for neutrals)
OLLAMA_MODEL=gemma4:26b python3 -m analytics.run refresh --classifier hybrid
#   --since-days 90  limits the (slow) LLM pass to recent messages; older -> lexicon
#   switching backend reclassifies all events; the run checkpoints every 300 msgs
#   (resumable) and the choice is remembered so later refreshes stay incremental

# peek from the CLI
python3 -m analytics.run skill --horizon 5 --min-calls 8
python3 -m analytics.run signals
python3 -m analytics.run clusters

# the dashboard  (use `python3 -m streamlit`, the bare `streamlit` shebang is wrong here)
python3 -m streamlit run dashboard/app.py     # has a "Refresh data" button too
#   then open http://localhost:8501
```

### Derived tables (all in godel_rest.db)

- `prices` / `price_fetch_log` — daily OHLCV cache + per-ticker fetch span.
- `events` — one row per (message, ticker) with `direction` (-1/0/+1).
- `event_returns` — `ret_{1,5,20}d` and market-adjusted `abn_{1,5,20}d`.

### Incremental design

Re-running `refresh` only: classifies **new** messages, fetches **uncached**
ticker spans, and computes returns for events whose window has closed. Skill /
signals / clusters are derived on demand, so they always reflect the latest data.

### Honest caveats (baked into the UI)

- **Direction labels are lexicon-based** — the weakest link. Swap `sentiment.classify`
  for FinBERT or a local Ollama pass to materially improve every downstream metric.
- Skill = predictive-of-price, **not** real P&L. Reporting bias (winners shouted,
  losers buried) is real.
- Shrinkage guards small samples but does not fix look-ahead across the whole
  panel — validate top users **out-of-sample** before trusting them.
- Many microcap tickers aren't tradeable size.

## ToS caveat

Replaying the session token to pull these feeds likely violates Gödel
Terminal's ToS. Operational/legal risk is the user's call.
