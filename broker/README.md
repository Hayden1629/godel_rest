# broker — Schwab API client + portfolio risk

A clean refactor of the `algobot_v2` Schwab harness, focused on reading your
brokerage data and analyzing portfolio risk/beta by pairing it with the Massive
price API.

## What changed vs algobot_v2

- **Config via `godel/.env`** instead of a gitignored `constants/parameters.py`.
- **One `BaseClient`** owns auth headers, token refresh-on-401, and retry —
  the per-client `_update_headers` duplication is gone.
- **Errors surface** as `SchwabAPIError` instead of being swallowed into `{}`.
- **Lazy token refresh** (no background daemon thread to leak).
- **New: `portfolio.py` + `analytics/portfolio_risk.py`** — holdings, exposure,
  and beta/vol/concentration vs SPY using Massive history.
- Typed, small modules; a single CLI.

## Setup — one paste-driven script

```bash
cd godel_rest
python3 -m broker.setup_auth
```

It prompts for your Schwab **app key/secret** (from the developer portal) if they
aren't already in `godel/.env` and saves them, asks for your **callback URL**
(default `https://127.0.0.1`, must match your app registration), then opens the
authorize page. Log in, approve, and paste the full redirect URL
(`https://127.0.0.1/?code=...`) back at the prompt. Tokens are stored at
`~/.godel_rest/schwab_tokens.json` (0600); the 7-day refresh token auto-renews
access tokens. Re-run only when that expires.

## Use

```bash
python3 -m broker.cli auth          # token status
python3 -m broker.cli balances      # cash, equity, buying power
python3 -m broker.cli positions     # open positions (normalized)
python3 -m broker.cli portfolio     # holdings + gross/net exposure
python3 -m broker.cli risk          # portfolio beta, vol, concentration (Massive)
python3 -m broker.cli quote NVDA    # live quote
python3 -m broker.cli hours         # is the market open
# orders require an explicit --confirm:
python3 -m broker.cli order AAPL BUY 10 --confirm
python3 -m broker.cli order AAPL BUY 10 --limit 190 --confirm
```

## Risk output

```bash
python3 -m broker.cli risk          # human report (default)
python3 -m broker.cli risk --json   # structured JSON for agents / scripts
python3 -m broker.cli risk --lookback 180
```

The **human report** shows market exposure (portfolio beta, $-equivalent, and
the SPY hedge to neutralize it), annual vol + diversification ratio, the book
(gross/net/long/short/cash), a positions table (weight, beta, R², vol, corr,
beta-contribution, % of portfolio risk), a **correlation matrix** across
holdings, and **flags** for unreliable betas (low R²) or outlier-driven vol.

The **`--json`** output carries the same data for an AI agent to review the
portfolio, reason over correlations, and manage beta exposure:
`{equity, portfolio:{beta, hedge_spy_notional, hedge_spy_shares, annual_vol,
diversification_ratio, gross/net/long/short, concentration_hhi, ...},
positions:[{beta, r2, annual_vol, corr_spy, beta_contribution, pct_risk,
low_confidence}], correlations:{labels, matrix}, flags:[...]}`.

Notes: per-position beta uses each name's own overlap with SPY; the correlation
matrix and risk decomposition use the common-date intersection. Prices are
cached in the `prices` table, so the first run warms the cache (~seconds) and
later runs are instant. Short positions contribute negative beta; equity
holdings only.

## Modules

```
broker/
  config.py       creds + endpoints from .env
  tokens.py       storage + refresh + TokenManager (lazy, thread-safe)
  setup_auth.py   one-time interactive OAuth
  base.py         BaseClient: auth, refresh-on-401, retry, typed errors
  accounts.py     balances, positions, open orders
  market_data.py  quotes, market hours
  orders.py       market/limit orders, cancel
  portfolio.py    holdings + exposure snapshot
  cli.py          command-line entry
analytics/portfolio_risk.py   beta/vol/concentration via Massive prices
```

## Caveat

Order placement moves real money. The CLI refuses to place orders without
`--confirm`; double-check symbol/side/quantity.
