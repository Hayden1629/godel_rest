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

1. **Install deps:** `pip install -r requirements.txt`. The token relay +
   pollers only need `requests`; the analytics/model layer adds pandas, numpy,
   statsmodels, matplotlib, streamlit, openpyxl and loguru.
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
python3 -m server.cli ratio AMD SOX                   # regress AMD on SOX, 6mo: beta, r2, std err, corr
python3 -m server.cli ratio AMD SOX --months 12 --json   # 12mo window, JSON (--raw adds timeseries)
python3 -m server.cli pair PLTR HIMS                  # beta-neutral pair-trade plan (long/short + share sizing)
python3 -m server.cli pair AMD NVDA --capital 50000 --benchmark SPY --entry 2 --json
python3 -m server.cli prices AMD                      # historical OHLCV, 6mo daily (table)
python3 -m server.cli prices AMD --resolution 1W --months 12 --json   # weekly, JSON (programmatic)
python3 -m server.cli prices AMD --months 6 --excel amd.xlsx          # write .xlsx (or --csv amd.csv)
python3 -m server.cli discover    # endpoints the monitor saw, grouped by command
python3 -m server.cli discover --command FA --status 404             # filter the capture
python3 -m server.cli raw /api/v1/trending --param timeframe=24H     # poke any path
```

### Terminal commands (`server.terminal`)

Run the terminal's own mnemonics from the shell — the same data the app pulls
under each command, printed (or `--json`). Word order is flexible: `DES BBY` or
the terminal-native `BBY EQ DES`.

```bash
python3 -m server.terminal DES BBY                       # company overview
python3 -m server.terminal FA BSX --statement balance_sheet --quarters 6
python3 -m server.terminal EM BBY                        # earnings surprises
python3 -m server.terminal ERN BBY                       # forward EPS estimates
python3 -m server.terminal SI BBY                        # short interest
python3 -m server.terminal ANR BBY                       # analyst ratings
python3 -m server.terminal TREND                         # trending tickers
python3 -m server.terminal MOST --tab GAINERS --limit 25 # most active/gainers/losers
python3 -m server.terminal IPO                           # upcoming + recent IPOs
python3 -m server.terminal DES BBY --json [--raw]        # machine-readable
```

Working: **DES, FA, EM, ERN, SI, ANR, TREND, MOST, IPO** (endpoints verified
live). `TAS`, `HALT`, `TRAN` aren't wired yet — their endpoints weren't found by
probing; capture them with the monitor (below), then wire them in
`server/commands.py`. The full endpoint map is in
[`API_ENDPOINTS.md`](API_ENDPOINTS.md).

Chat options: `--channels <titles-or-uuids> --interval 3 --size 50`
(omit `--channels` to stream just the default `options` channel).
News options: `--important --interval 30`.

### Channels

63 channels total. The valuable community rooms are `type=user_write`:
`general, biotech, options, sellside, buyside, quant, smallcaps, crypto, fx, bonds, energy, politics, …` plus `paid` (`user_only`) and `pikers`
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

The extension monitors every `api.` call the app makes and records, per call,
the **terminal command in effect** (correlated from `/api/v1/command-action`),
the **HTTP status**, and a **response-body sample**. To find the endpoint for a
command not yet wired up (e.g. TAS, HALT, TRAN): reload the extension, open that
window / run that command in the terminal, then:

```bash
python3 -m server.cli discover                 # grouped by command context
python3 -m server.cli discover --command TAS   # just that command's calls
python3 -m server.cli discover --status 404    # what broke
```

Capture files: `~/.godel_rest/endpoints.log` (human-readable),
`~/.godel_rest/endpoints.jsonl` (full detail), `~/.godel_rest/samples/`
(one response body per command+endpoint, for inspecting a new payload's shape).

## Data

SQLite at `godel_rest/godel_rest.db`:

- `chat_messages` — one row per message (idempotent on int64 `id`).
- `chat_tickers` — expanded `[TICKER:seriesId]` mentions joined to
  `seriesContexts` (ticker, name, figi, isin, cik, series_type).
- `news_items` — normalized news (source, type, time, important, content,
  symbols), raw payload kept.

## Status / next

- [X] Extension token relay (MAIN-world hook) + local receiver + SQLite sink.
- [X] **Task 1 confirmed** — `verify` returns 200 server-side, no Cloudflare.
- [X] Chat poller (multi-channel, dedup, ticker tagging). Tested on 4 channels.
- [X] Channel enumeration via `/api/chat/channels` (63 channels mapped).
- [X] News poller — `/api/v1/top-news-items` (`?important=true` supported).
- [X] Trending — `/api/v1/trending?timeframe=24H` (ranked by mentions).
- [X] Endpoint monitor in the extension (now captures command + status + sample).
- [X] Text commands — **DES/FA/EM/ERN/SI/ANR/TREND/MOST/IPO** live in
  `server.terminal` (`server/commands.py`). See [`API_ENDPOINTS.md`](API_ENDPOINTS.md).
- [X] FA migrated to the new `financial-metric-group` endpoint (old
  `consolidated_financials` route was retired → 404).
- [ ] Breaking news — old `/api/fetchBreaking` is 404. Find the replacement via
  `discover` (open the News/breaking window in the app).
- [ ] TAS / HALT / TRAN — endpoints not found by probing; capture with `discover`.
- [ ] Cursor param for history backfill (response gives `prevCursor`; test
  `?cursor=`/`?before=`/`?prevCursor=` via `raw`).
- [ ] Confirm `createdAt` is UTC.

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

## Financial statements + reverse-DCF model

Type a ticker, pull its quarterly **income statement, balance sheet and cash
flow** straight from Gödel (`<TICKER> EQ FA` under the hood), chart the standard
modeling set, compute **real free cash flow** (operating cash flow − capex), and
run an **EV-based reverse DCF** that solves for the FCF growth rate the market is
pricing in.

```
analytics/
  financials.py  Gödel financial-metric-group -> tidy period frame + Holt-Winters
                 forecast + trend charts  (fetch_financials / plot_trends)
  model.py       merges the 3 statements, derives FCF/margins, builds enterprise
                 value, runs the reverse DCF, assembles the company card
  research.py    price summary/technicals + Massive fundamentals + chart
  massive.py     Massive API: company details (name/shares/desc) + fundamentals
```

### CLI

```bash
# full model: downloads 3 statements (CSV+JSON to output/), charts, reverse DCF
python3 -m analytics.run model BSX
python3 -m analytics.run model BSX --period ANN --forecast 12 \
        --discount-rate 0.10 --terminal-growth 0.025 --years 10

# just one statement's trends + forecast (ticker or a downloaded .xlsx path)
python3 -m analytics.run fa BSX --statement cash_flow --period QTR --table
```

### Bulk screener (whole S&P 500 → sortable web page)

Runs the model across an entire universe on a lean path (no charts, no
per-ticker files) and writes a **CSV** plus a **self-contained interactive HTML**
table you sort/filter in the browser — click any column to rank by it, filter by
sector or ticker.

```bash
python3 -m analytics.run screen                       # full S&P 500 (~90s, 8 workers)
python3 -m analytics.run screen --limit 25            # quick smoke test
python3 -m analytics.run screen --workers 8 --out output/sp500_screen
python3 -m analytics.run screen --tickers-file my_list.txt   # custom universe
open output/sp500_screen.html                         # sort/filter in a browser
```

Columns: price, market cap, EV, TTM FCF, EV/FCF, implied vs historical FCF
growth (and the gap), revenue YoY, gross/net margin. The constituent list is
pulled from a public dataset and cached under `analytics/data/`. Reverse-DCF is
not meaningful for banks/insurers/REITs (no clean FCF) — the page flags that.

`model BSX` prints price / market cap / EV / TTM FCF / EV·FCF / implied-vs-
historical FCF growth, and saves charts + statement files under `output/`.

**How EV + FCF are sourced:**

| input                             | source                                                                                                         |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 3 statements                      | Gödel`GET /api/v1/terminal/financial-metric-group/legal-entities/{legalEntityId}?instrumentId={instrumentId}` (all 3 statements in one call) |
| share price (last close)          | Gödel`GET /api/v1/search?query=TICKER`                                                                      |
| shares outstanding, name, summary | Massive`GET /v3/reference/tickers/{T}`                                                                       |
| price history → beta / YTD / vol | Massive`GET /v2/aggs/...` (ticker + SPY)                                                                     |

`EV = price×shares + net debt` (debt − cash from the balance sheet). `FCF = operating cash flow − capex`. The reverse DCF bisects for the constant FCF
growth that makes the discounted TTM-FCF stream equal EV, then compares it to
the historical FCF CAGR.

> **Endpoint migration (Aug 2026):** the old
> `/api/v1/consolidated_financials/{statement}/{series_id}/{period}` route was
> retired (now 404). The replacement returns all three statements as flat
> records keyed by `seriesTypeId`, with a self-describing `financialMetricTypes`
> id→name catalog, in **raw dollars** (the old route was in millions).
> `financials.py` maps `seriesTypeId → slug`, scales monetary/share values ÷1e6,
> aligns line items on `(fiscalYear, fiscalPeriod)`, and resolves the
> `legalEntityId`/`instrumentId` from `/api/v2/company-profile/{seriesId}`. See
> [`API_ENDPOINTS.md`](API_ENDPOINTS.md). Coverage gaps still happen — some names
> report only a balance sheet, so FCF/reverse-DCF fall back to balance-sheet trends.

### Interactive dashboard

```bash
python3 -m streamlit run dashboard/model_app.py   # then open http://localhost:8501
```

A ticker search box → company name + summary, a stat row (beta vs SPY, YTD, 1Y,
vol, 52-week range), price+SMA chart, valuation cards, per-metric trend/forecast
charts (only metrics with data), raw statement tabs with CSV downloads, and a
**"🔧 Under the hood"** expander that lists every API call made for the ticker.

> Streamlit caches imported submodules. After editing anything under
> `analytics/` or `server/`, **restart the server** — its hot-reload only re-runs
> the script, so newly added functions raise `AttributeError` until a restart.

### API call logging

Every Gödel and Massive call is logged via [loguru](https://loguru.readthedocs.io)
to `~/.godel_rest/api.log` (rotating) and kept in an in-memory ring buffer
(`server.api_client.recent_calls()`) that the dashboard renders. The `apiKey`
query param is stripped before anything is buffered or written. Logs go to the
file only, so the CLI stays quiet.

```bash
tail -f ~/.godel_rest/api.log
```

## Research reports (RES)

Downloads the sell-side research PDFs your Gödel subscription entitles you to
(analyst notes, "First Take"s, sector strategy pieces, etc.), tracked in their
own SQLite file so the catalog and download state stay separate from the chat
archive.

```
server/research.py   listing (public Supabase table, no login) + PDF download
                      (app.godelterminal.com/api/fetchResearch, session-cookie
                      auth reusing the same token the extension already relays)
research.db           one row per report: metadata + downloaded/pdf_path/error
```

```bash
# 1. build/refresh the catalog (metadata only, no PDFs -- cheap, ~10-20 min for
#    all ~850k rows). Safe to re-run anytime to pick up newly published reports.
python3 -m server.cli research-sync

python3 -m server.cli research-status     # totals: downloaded / pending / errored

# 2. download PDFs for everything not yet downloaded, newest first. Resumable
#    (already-downloaded rows are skipped) and self-limiting: stops before
#    filling the disk instead of crashing mid-write.
nohup python3 -m server.cli research-backfill --delay 0.5 > research_backfill.log 2>&1 &
tail -f research_backfill.log

# just test on a couple ids first, or cap a run:
python3 -m server.cli research-fetch 907603 907602
python3 -m server.cli research-backfill --limit 20
python3 -m server.cli research-backfill --min-free-gb 10   # bigger safety floor
```

### Where it writes

PDFs land under `<PDF_DIR>/<year>/<month>/`. The catalog spans 1982-2026 and
recent years hold 50k+ reports each, so month shards keep directories to a few
thousand files; look reports up via `research.db`'s `pdf_path`, not by browsing.

Both paths are environment-overridable, so the corpus can live on a big
external volume while the repo stays on the system disk:

```bash
export GODEL_RESEARCH_DIR=/media/<you>/<DRIVE>/godel_research/research_pdfs
export GODEL_RESEARCH_DB=/path/to/repo/research.db   # default: repo root
```

Keep `research.db` on a real local filesystem. SQLite locking is unreliable on
NTFS/exFAT/network mounts, and the backfill writes a row per completed
download -- the PDFs are fine out there, the database is not.

**Sizing.** A 32-report sample spread across the catalog measured mean 1.1MB /
median 528KB per PDF -- the distribution is heavily right-skewed (a 4MB
strategy deck against a 200KB one-page note). Against 878,686 reports that
projects to roughly **475GB (median) to 1.0TB (mean)**, so "the full catalog"
does not reliably fit on a 1TB volume. An earlier ~300KB/report estimate in
these docs was measured on too small a sample and was low by ~3x.

Re-measure as you go rather than trusting either number:

```bash
find "$GODEL_RESEARCH_DIR" -name '*.pdf' | wc -l && du -sh "$GODEL_RESEARCH_DIR"
```

`--min-free-gb` is the actual safety net: the run stops cleanly at the floor
instead of filling the volume, and resumes later. If the whole corpus won't
fit, narrow it rather than racing the disk -- the catalog carries `provider`,
`date`, `region`, `sector` and `GICS`, so a filtered subset is usually the
better target.

### Throughput

`research-backfill` downloads with `--workers` threads (default 8) and each
thread reuses one keep-alive connection. Pacing is server-driven: a 429 or 503
parks *every* worker for the `Retry-After` interval (exponential backoff if the
header is absent) rather than each thread rediscovering the limit. `--delay`
adds an optional extra pause per download and defaults to 0.

```bash
python3 -m server.cli research-backfill --workers 8      # measured 4.6 reports/sec
python3 -m server.cli research-backfill --workers 2      # gentle
python3 -m server.cli research-backfill --workers 16     # watch the log for 429s
```

8 workers measured 4.6 reports/sec against the live API with no 429s, which is
~53 hours for the full catalog -- so this is a multi-day job you start in
`nohup` and check on, not something that finishes overnight. Scaling is
sub-linear (these are 1MB PDFs; bandwidth starts to bind before the API does).

Raise workers only while watching the log. Sustained 429s mean the backoff is
doing all the work and the extra threads are buying nothing; a 403 that doesn't
clear usually means Cloudflare stopped believing the session, not that the
token expired.

Failed reports keep `downloaded=0` and record `download_error`, so a later run
retries them. Reports that fail permanently will be retried on every full run
-- find them with:

```sql
SELECT id, download_error FROM research_reports WHERE download_error IS NOT NULL;
```

## ToS caveat

Replaying the session token to pull these feeds likely violates Gödel
Terminal's ToS; for RES specifically, a paid subscription is what entitles you
to download these reports in the first place (same action the UI's download
button performs, just automated) -- but bulk-downloading the entire catalog at
once is a different scale of use than clicking through individually, so treat
this as the same user-call tradeoff as chat/news, not a green light to
redistribute the PDFs anywhere.
