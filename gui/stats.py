"""Aggregates for the Stats view. Timestamps are UTC ISO strings (two formats,
both sort correctly by prefix), so day/hour buckets come from string slices and
use the created_at index. The full-table pieces (per-channel totals, ticker
mentions) take seconds on ~1M rows, so `overview` is cached for STATS_TTL_S."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import state

STATS_TTL_S = 300
DAILY_TTL_S = 60
TOP_N = 15
DAY_CHOICES = (30, 90, 180)
RECENT_DAYS = 7


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _count_since(conn: sqlite3.Connection, since: float, until: float | None = None) -> int:
    sql = "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?"
    args = [_utc_iso(since)]
    if until is not None:
        sql += " AND created_at < ?"
        args.append(_utc_iso(until))
    return conn.execute(sql, args).fetchone()[0]


def tiles(conn: sqlite3.Connection, now: float, total: int) -> dict:
    day = 86400
    week_count = _count_since(conn, now - RECENT_DAYS * day)
    active = conn.execute(
        "SELECT COUNT(DISTINCT username) FROM chat_messages WHERE created_at >= ?",
        (_utc_iso(now - RECENT_DAYS * day),),
    ).fetchone()[0]
    # compare with the same 24h a week earlier: chat volume swings hard between
    # weekdays and weekends, so "vs the previous day" says little on a Monday
    return {
        "last_24h": _count_since(conn, now - day),
        "week_ago_24h": _count_since(conn, now - 8 * day, now - 7 * day),
        "avg_per_day_7d": round(week_count / RECENT_DAYS),
        "active_users_7d": active,
        "total": total,
    }


def daily(conn: sqlite3.Connection, now: float, days: int,
          channel_id: str | None = None) -> list[dict]:
    """Messages per UTC day for the last `days` days, zero-filled, oldest first."""
    if days not in DAY_CHOICES:
        raise ValueError(f"days must be one of {DAY_CHOICES}")
    today = datetime.fromtimestamp(now, timezone.utc).date()
    start = today - timedelta(days=days - 1)
    sql = ("SELECT substr(created_at, 1, 10) AS d, COUNT(*) FROM chat_messages"
           " WHERE created_at >= ?")
    args: list = [start.isoformat()]
    if channel_id:
        sql += " AND channel_id = ?"
        args.append(channel_id)
    sql += " GROUP BY d"
    counts = dict(conn.execute(sql, args).fetchall())
    return [
        {"day": (start + timedelta(days=i)).isoformat(),
         "count": counts.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(days)
    ]


def hourly(conn: sqlite3.Connection, now: float, days: int = 30) -> list[int]:
    """Messages per UTC hour-of-day over the last `days` days (24 ints)."""
    rows = conn.execute(
        "SELECT CAST(substr(created_at, 12, 2) AS INTEGER) AS h, COUNT(*)"
        " FROM chat_messages WHERE created_at >= ? GROUP BY h",
        (_utc_iso(now - days * 86400),),
    ).fetchall()
    out = [0] * 24
    for h, n in rows:
        if h is not None and 0 <= h < 24:
            out[h] = n
    return out


def channels(conn: sqlite3.Connection, now: float, titles: dict) -> list[dict]:
    """Every channel with stored messages: all-time total, last 7d, newest message."""
    recent = dict(conn.execute(
        "SELECT channel_id, COUNT(*) FROM chat_messages WHERE created_at >= ?"
        " GROUP BY channel_id",
        (_utc_iso(now - RECENT_DAYS * 86400),),
    ).fetchall())
    rows = conn.execute(
        "SELECT channel_id, COUNT(*), MAX(created_at) FROM chat_messages"
        " GROUP BY channel_id ORDER BY 2 DESC"
    ).fetchall()
    return [
        {"id": cid, "title": titles.get(cid) or (cid or "?")[:8], "total": total,
         "last_7d": recent.get(cid, 0), "newest_ms": state.to_epoch_ms(newest)}
        for cid, total, newest in rows
    ]


def top_tickers(conn: sqlite3.Connection, now: float, limit: int = TOP_N) -> list[dict]:
    """Most-mentioned tickers in the last 7 days. Message ids grow with time, so
    the window becomes an id range on chat_tickers' (message_id, …) primary key."""
    first = conn.execute(
        "SELECT MIN(id) FROM chat_messages WHERE created_at >= ?",
        (_utc_iso(now - RECENT_DAYS * 86400),),
    ).fetchone()[0]
    if first is None:
        return []
    rows = conn.execute(
        "SELECT ticker, COUNT(*) FROM chat_tickers WHERE message_id >= ?"
        " GROUP BY ticker ORDER BY 2 DESC LIMIT ?",
        (first, limit),
    ).fetchall()
    return [{"ticker": t, "name": _ticker_name(conn, t), "count": c} for t, c in rows]


def _ticker_name(conn: sqlite3.Connection, ticker: str) -> str | None:
    """Most common stored name — some rows carry junk like 'xyz:NVDA'."""
    row = conn.execute(
        "SELECT name FROM chat_tickers WHERE ticker = ? AND name IS NOT NULL"
        " GROUP BY name ORDER BY COUNT(*) DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def top_users(conn: sqlite3.Connection, now: float, limit: int = TOP_N) -> list[dict]:
    rows = conn.execute(
        "SELECT username, COUNT(*) FROM chat_messages WHERE created_at >= ?"
        " GROUP BY username ORDER BY 2 DESC LIMIT ?",
        (_utc_iso(now - RECENT_DAYS * 86400), limit),
    ).fetchall()
    return [{"username": u or "?", "count": c} for u, c in rows]


def overview(conn: sqlite3.Connection, now: float, titles: dict) -> dict:
    chans = channels(conn, now, titles)
    return {
        "generated_ms": int(now * 1000),
        "tiles": tiles(conn, now, sum(c["total"] for c in chans)),
        "hourly_utc": hourly(conn, now),
        "channels": chans,
        "tickers_7d": top_tickers(conn, now),
        "users_7d": top_users(conn, now),
    }


class StatsCache:
    """overview() every STATS_TTL_S; daily() per (days, channel) every DAILY_TTL_S."""

    def __init__(self):
        self._overview = state.TtlCache(STATS_TTL_S)
        self._daily: dict[tuple, tuple[float, list]] = {}

    def overview(self, titles: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def compute():
            conn = state.connect()
            try:
                return overview(conn, now, titles)
            finally:
                conn.close()

        return self._overview.get(compute, now)

    def daily(self, days: int, channel_id: str | None, now: float | None = None) -> list:
        now = time.time() if now is None else now
        key = (days, channel_id)
        hit = self._daily.get(key)
        if hit and now - hit[0] < DAILY_TTL_S:
            return hit[1]
        conn = state.connect()
        try:
            rows = daily(conn, now, days, channel_id)
        finally:
            conn.close()
        self._daily = {**self._daily, key: (now, rows)}
        return rows
