# Gödel Terminal API — reverse-engineered endpoints

Base host: `https://api.godelterminal.com`. Auth: `Authorization: Bearer <JWT>`
(captured by the browser extension → `server/token_store`). Backend is Spring
Boot (`/actuator` is live; most management routes are Cloudflare-blocked).

Resolve a ticker to a `seriesId` first:
`GET /api/v1/search?query=<TICKER>` → `instruments[].primary_series_id`
(exact ticker match preferred).

## Command → endpoint map

| Cmd | Endpoint | Notes |
|-----|----------|-------|
| **DES** | `GET /api/v2/company-profile/{seriesId}` | overview, multiples, dividend, entity ids |
| **FA** | `GET /api/v1/terminal/financial-metric-group/legal-entities/{legalEntityId}?instrumentId={instrumentId}` | **replaced** the retired `consolidated_financials` route (now 404). All 3 statements in one call. |
| **EM** | `GET /api/v1/earnings?seriesId={id}` | reported EPS vs estimate + surprise% |
| **ERN** | `GET /api/v1/earnings-estimates?seriesId={id}` | forward EPS estimates |
| **SI** | `POST /api/v1/shortinterest {"seriesId":id}` | short interest + days-to-cover + time series |
| **ANR** | `GET /api/v1/analyst-ratings?seriesId={id}` | rating actions |
| **TREND** | `GET /api/v1/trending?timeframe=24H` | trending tickers by mentions |
| **MOST** | `GET /api/most-v2?tabType=ACTIVE&rows=N&sector=All&minMarketCap=..&maxMarketCap=..` | ACTIVE / GAINERS / LOSERS |
| **IPO** | `GET /api/ipos` | upcoming + recent IPOs |
| **N** | `POST /api/news/items {"size":N}` | news feed |

Other confirmed-live routes seen from the app: `/api/v1/aggregates` (POST),
`/api/tv-advanced/bars` (POST, charting/HP), `/api/v1/ratio-analysis`,
`/api/v1/watchlists`, `/api/sectors`, `/api/v1/instruments?subset=..`,
`/api/notifications`, `/api/chat/channels`.

### Not yet discovered — capture with the monitor
`TAS` (time & sales), `HALT` (market halts), `TRAN` (transcripts) did not
respond to blind path probing. Capture them live:

1. `python -m server.token_server`
2. load the extension, open the app, run the command (e.g. `<TICKER> TAS`)
3. `python -m server.cli discover --command <name>` — the monitor now records
   the command context, HTTP status, and a response sample per call
   (`~/.godel_rest/endpoints.jsonl` + `~/.godel_rest/samples/`).

## FA endpoint details

The response is **self-describing** — no external metric dictionary needed:

- `financialMetrics[]`: flat records
  `{seriesTypeId, periodEnd, periodicity, fiscalYear, fiscalPeriod, asOf, actual, estimate*}`
- `financialMetricTypes[]`: `{id, name, description}` — the `seriesTypeId → name` catalog
- `currency`, `multipleColumns` (pe/ps/pb/pcf)

Units are **raw dollars / raw share counts** (the old route was in millions).
`analytics/financials.py` scales monetary + share-count values ÷1e6 on ingest
(EPS left as-is) and aligns line items on `(fiscalYear, fiscalPeriod)` because
`periodEnd` can drift a few days between items in the same quarter.
`legalEntityId` + `instrumentId` come from
`company-profile.marketCapSeriesIdsAndShareCounts[].seriesContext.{legalEntity,instrument}.id`.

`periodicity` values: `QUARTERLY`, `ANNUAL`, `SEMIANNUAL`
(CLI `--period QTR|ANN`).

## Running commands

```
python -m server.terminal DES BBY          # or: BBY EQ DES
python -m server.terminal FA BSX --statement balance_sheet
python -m server.terminal EM BBY
python -m server.terminal MOST --tab GAINERS
python -m server.terminal TREND
python -m server.terminal <CMD> ... --json [--raw]
```
