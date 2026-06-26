"""Daily price bars from the Massive API (Polygon-compatible), cached in SQLite.

Endpoint:
    GET https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}?apiKey=...

Cache is incremental: we only fetch the date span we don't already have for a
ticker, so re-runs as new chat arrives are cheap.
"""
import sqlite3
import time
from datetime import date, datetime

import requests

from .config import DB_PATH, massive_api_key

BASE = "https://api.massive.com"

PRICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker  TEXT,
    date    TEXT,          -- YYYY-MM-DD (US/Eastern trading day)
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  REAL,
    vwap    REAL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);

CREATE TABLE IF NOT EXISTS price_fetch_log (
    ticker     TEXT PRIMARY KEY,
    from_date  TEXT,
    to_date    TEXT,
    fetched_at TEXT,
    status     TEXT          -- ok | empty | error:<msg>
);
"""


def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.executescript(PRICE_SCHEMA)
    return c


def fetch_bars(ticker: str, frm: str, to: str, timeout: int = 30) -> list[dict]:
    """Raw daily bars for a ticker over [frm, to] (YYYY-MM-DD), oldest-first."""
    import time
    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000,
              "apiKey": massive_api_key()}
    start = time.perf_counter()
    status: int | str = "ERR"
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
        status = r.status_code
        if r.status_code == 429:
            raise RuntimeError("massive 429 rate limited")
        r.raise_for_status()
        return r.json().get("results") or []
    finally:
        try:
            from server.api_client import record
            record("GET", path, {"frm": frm, "to": to}, status,
                   (time.perf_counter() - start) * 1000, host="massive")
        except Exception:
            pass


def _store(conn, ticker: str, bars: list[dict]) -> int:
    rows = []
    for b in bars:
        d = datetime.utcfromtimestamp(b["t"] / 1000).strftime("%Y-%m-%d")
        rows.append((ticker, d, b.get("o"), b.get("h"), b.get("l"),
                     b.get("c"), b.get("v"), b.get("vw")))
    conn.executemany(
        """INSERT OR REPLACE INTO prices
           (ticker,date,open,high,low,close,volume,vwap)
           VALUES (?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


def ensure_prices(tickers, frm: str, to: str | None = None,
                  delay: float = 0.0, log=print) -> dict:
    """Ensure each ticker has bars covering [frm, to]. Skips tickers already
    fetched for a span covering the request. Returns a small summary."""
    to = to or date.today().strftime("%Y-%m-%d")
    conn = _conn()
    done = {r[0]: (r[1], r[2]) for r in
            conn.execute("SELECT ticker, from_date, to_date FROM price_fetch_log")}
    fetched = skipped = errors = 0
    tickers = sorted(set(tickers))
    for i, t in enumerate(tickers, 1):
        prev = done.get(t)
        if prev and prev[0] <= frm and prev[1] >= to:
            skipped += 1
            continue
        try:
            bars = fetch_bars(t, frm, to)
            n = _store(conn, t, bars)
            status = "ok" if n else "empty"
            fetched += 1
            if i % 25 == 0 or n == 0:
                log(f"[prices] {i}/{len(tickers)} {t}: {n} bars ({status})")
        except Exception as e:
            status = f"error:{e}"
            errors += 1
            log(f"[prices] {t}: {e}")
        conn.execute(
            """INSERT OR REPLACE INTO price_fetch_log
               (ticker, from_date, to_date, fetched_at, status)
               VALUES (?,?,?,?,?)""",
            (t, frm, to, datetime.utcnow().isoformat(timespec="seconds"), status))
        conn.commit()
        if delay:
            time.sleep(delay)
    conn.close()
    summary = {"requested": len(tickers), "fetched": fetched,
               "skipped": skipped, "errors": errors}
    log(f"[prices] done: {summary}")
    return summary
