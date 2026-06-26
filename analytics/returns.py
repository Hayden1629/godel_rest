"""Join forward returns onto each event.

Entry rule (avoids look-ahead): a mention at time `ts` is entered at the close
of the FIRST trading day strictly after `ts`. Forward return at horizon h is
close[entry+h]/close[entry] - 1. Abnormal return subtracts the benchmark (SPY)
return over the identical date window.

Incremental: computes rows missing from `event_returns`, and recomputes rows
whose longest-horizon value is still NULL (their window may have since closed as
new prices arrived).
"""
import sqlite3
from bisect import bisect_right

from .config import DB_PATH, HORIZONS, MARKET_BENCHMARK

RETURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_returns (
    message_id INTEGER,
    series_id  INTEGER,
    ticker     TEXT,
    entry_date TEXT,
    ret_1d  REAL, ret_5d  REAL, ret_20d  REAL,
    abn_1d  REAL, abn_5d  REAL, abn_20d  REAL,
    PRIMARY KEY (message_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_eret_ticker ON event_returns(ticker);
"""


def _series(conn, ticker):
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE ticker=? AND close IS NOT NULL "
        "ORDER BY date", (ticker,)).fetchall()
    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    return dates, closes


def _fwd(dates, closes, entry_idx, h):
    j = entry_idx + h
    if j >= len(closes) or closes[entry_idx] in (None, 0):
        return None
    return closes[j] / closes[entry_idx] - 1.0


def build(log=print) -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(RETURNS_SCHEMA)
    maxh = max(HORIZONS)

    # events needing computation: not present, or longest horizon still NULL
    todo = conn.execute(
        f"""SELECT e.message_id, e.series_id, e.ticker, e.ts
            FROM events e
            LEFT JOIN event_returns r
              ON r.message_id = e.message_id AND r.series_id = e.series_id
            WHERE r.message_id IS NULL OR r.ret_{maxh}d IS NULL""").fetchall()
    if not todo:
        conn.close()
        log("[returns] nothing to do")
        return 0

    # benchmark series
    bdates, bclose = _series(conn, MARKET_BENCHMARK)
    bidx = {d: i for i, d in enumerate(bdates)}

    # group events by ticker to load each price series once
    by_ticker: dict[str, list] = {}
    for mid, sid, ticker, ts in todo:
        by_ticker.setdefault(ticker, []).append((mid, sid, ts))

    out = []
    for ticker, evs in by_ticker.items():
        dates, closes = _series(conn, ticker)
        if not dates:
            continue
        for mid, sid, ts in evs:
            mention_day = (ts or "")[:10]
            # first trading day strictly after the mention day
            entry_idx = bisect_right(dates, mention_day)
            if entry_idx >= len(dates):
                continue
            entry_date = dates[entry_idx]
            rets = {h: _fwd(dates, closes, entry_idx, h) for h in HORIZONS}
            abns = {}
            bi = bidx.get(entry_date)
            for h in HORIZONS:
                abns[h] = None
                if rets[h] is None or bi is None:
                    continue
                bj = bi + h
                if bj < len(bclose) and bclose[bi]:
                    abns[h] = rets[h] - (bclose[bj] / bclose[bi] - 1.0)
            out.append((mid, sid, ticker, entry_date,
                        rets[1], rets[5], rets[20],
                        abns[1], abns[5], abns[20]))

    conn.executemany(
        """INSERT OR REPLACE INTO event_returns
           (message_id, series_id, ticker, entry_date,
            ret_1d, ret_5d, ret_20d, abn_1d, abn_5d, abn_20d)
           VALUES (?,?,?,?,?,?,?,?,?,?)""", out)
    conn.commit()
    conn.close()
    log(f"[returns] wrote {len(out)} event-return rows")
    return len(out)
