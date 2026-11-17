"""Stats view aggregates against a temp DB with known timestamps.

    .venv/bin/python -m pytest tests/test_gui_stats.py
"""
from datetime import datetime, timezone

import pytest

from gui import state, stats
from server.sink import Sink

NOW = datetime(2027, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp()
TICKER = {"ticker": "NVDA", "seriesId": 4427080, "name": "NVIDIA"}


@pytest.fixture
def conn(tmp_path):
    sink = Sink(db_path=tmp_path / "chat.db")
    rows = [
        # id, channel, created_at (two API formats), user
        (1, "gen", "2026-06-01T10:00:00.1234", "old"),          # outside every window
        (9, "gen", "2027-01-08T01:00:00.000000Z", "dave"),       # same 24h last week
        (10, "gen", "2027-01-10T09:30:00.000000Z", "alice"),
        (11, "bio", "2027-01-14T11:00:00.000000Z", "bob"),       # prev 24h
        (12, "gen", "2027-01-15T03:15:00.000000Z", "alice"),     # last 24h
        (13, "gen", "2027-01-15T03:45:00.000000Z", "carol"),     # last 24h
    ]
    for mid, ch, ts, user in rows:
        sink.save_chat_message({"id": mid, "chatChannelId": ch, "createdAt": ts,
                                "username": user, "content": ""}, [])
    for mid in (1, 12, 13):
        sink.conn.execute(
            "INSERT INTO chat_tickers (message_id, ticker, series_id, name) VALUES (?,?,?,?)",
            (mid, TICKER["ticker"], TICKER["seriesId"], TICKER["name"]))
    sink.conn.execute(
        "INSERT INTO chat_tickers (message_id, ticker, series_id, name) VALUES (13, 'AMD', 1, 'AMD')")
    sink.conn.commit()
    sink.close()
    c = state.connect(tmp_path / "chat.db")
    yield c
    c.close()


@pytest.mark.integration
def test_tiles(conn):
    t = stats.tiles(conn, NOW, total=6)
    assert t == {"last_24h": 2, "week_ago_24h": 1, "avg_per_day_7d": 1,
                 "active_users_7d": 3, "total": 6}


@pytest.mark.integration
def test_daily_zero_fills_and_filters(conn):
    rows = stats.daily(conn, NOW, 30)
    assert len(rows) == 30
    assert rows[-1] == {"day": "2027-01-15", "count": 2}
    assert rows[0]["day"] == "2026-12-17"
    by_day = {r["day"]: r["count"] for r in rows}
    assert by_day["2027-01-10"] == 1 and by_day["2027-01-12"] == 0
    only_bio = {r["day"]: r["count"] for r in stats.daily(conn, NOW, 30, "bio")}
    assert only_bio["2027-01-14"] == 1 and only_bio["2027-01-15"] == 0


@pytest.mark.unit
def test_daily_rejects_odd_ranges(conn):
    with pytest.raises(ValueError):
        stats.daily(conn, NOW, 7)


@pytest.mark.integration
def test_hourly_buckets_utc(conn):
    h = stats.hourly(conn, NOW)
    assert len(h) == 24
    assert h[1] == 1 and h[3] == 2 and h[9] == 1 and h[11] == 1 and sum(h) == 5


@pytest.mark.integration
def test_channels_totals_recent_and_titles(conn):
    chans = stats.channels(conn, NOW, {"gen": "general"})
    assert [c["id"] for c in chans] == ["gen", "bio"]
    gen, bio = chans
    assert gen["title"] == "general" and gen["total"] == 5 and gen["last_7d"] == 3
    assert bio["title"] == "bio" and bio["last_7d"] == 1
    assert gen["newest_ms"] == state.to_epoch_ms("2027-01-15T03:45:00Z")


@pytest.mark.integration
def test_top_tickers_window_excludes_old(conn):
    assert stats.top_tickers(conn, NOW) == [
        {"ticker": "NVDA", "name": "NVIDIA", "count": 2},
        {"ticker": "AMD", "name": "AMD", "count": 1},
    ]


@pytest.mark.integration
def test_top_users(conn):
    assert stats.top_users(conn, NOW)[0] == {"username": "alice", "count": 2}


@pytest.mark.integration
def test_overview_shape(conn):
    o = stats.overview(conn, NOW, {})
    assert o["tiles"]["total"] == 6
    assert set(o) == {"generated_ms", "tiles", "hourly_utc", "channels",
                      "tickers_7d", "users_7d"}
