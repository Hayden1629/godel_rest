"""Incremental refresh: chat tables -> events -> prices -> forward returns.

Idempotent and cheap to re-run as new chat arrives:
  * events: only new messages are classified.
  * prices: only tickers/spans not already cached are fetched.
  * returns: only events missing a (closed-window) return are computed.

The skill/signal/cluster/network views are derived on demand by the dashboard
from these tables, so they always reflect the latest refresh.
"""
import re
import sqlite3
from datetime import date

from . import events, prices, returns
from .config import DB_PATH, MARKET_BENCHMARK

# US-equity-looking tickers only (Massive stocks endpoint). Excludes crypto
# pairs like BTCUSD and foreign listings like TLO:CA.
EQUITY_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _equity_tickers(min_mentions: int) -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT ticker, COUNT(*) n FROM events GROUP BY ticker").fetchall()
    conn.close()
    out = []
    for t, n in rows:
        if n < min_mentions:
            continue
        if t.endswith("USD") or ":" in t:
            continue
        if EQUITY_RE.match(t):
            out.append(t)
    return out


def _earliest_event_date() -> str:
    conn = sqlite3.connect(str(DB_PATH))
    r = conn.execute("SELECT MIN(ts) FROM events").fetchone()[0]
    conn.close()
    return (r or "2025-01-01")[:10]


def refresh(min_ticker_mentions: int = 2, price_delay: float = 0.0,
            classifier: str | None = None, since_days: int | None = None,
            log=print) -> dict:
    log(f"[pipeline] building events (classifier={classifier}, "
        f"since_days={since_days})…")
    new_events = events.build(classifier=classifier, since_days=since_days, log=log)

    frm = _earliest_event_date()
    to = date.today().strftime("%Y-%m-%d")
    tickers = _equity_tickers(min_ticker_mentions)
    log(f"[pipeline] ensuring prices for {len(tickers)} equity tickers "
        f"+ {MARKET_BENCHMARK}, {frm}..{to}")
    psum = prices.ensure_prices([MARKET_BENCHMARK] + tickers, frm, to,
                                delay=price_delay, log=log)

    log("[pipeline] computing forward returns…")
    nret = returns.build(log=log)

    summary = {"new_events": new_events, "prices": psum, "return_rows": nret}
    log(f"[pipeline] refresh done: {summary}")
    return summary
