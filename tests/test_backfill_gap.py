"""backfill --gap stopping rule, against a fake paginated API.

    .venv/bin/python -m pytest tests/test_backfill_gap.py
"""
import pytest

from server import backfill
from server.sink import Sink

PAGE = 3


def _fake_api(ids: list[int]):
    """Pages oldest-first like the real API; beforeCursor = oldest id seen."""
    ordered = sorted(ids)

    def fetch_page(channel_id, size=PAGE, cursor=None, cursor_param=None):
        older = [i for i in ordered if cursor is None or i < cursor]
        page = older[-size:]
        content = [{"id": i, "chatChannelId": channel_id,
                    "createdAt": f"2026-01-01T00:00:{i:02d}Z", "content": ""} for i in page]
        return {"content": content, "prevCursor": page[0] if page else None}

    return fetch_page


@pytest.fixture
def sink(tmp_path):
    s = Sink(db_path=tmp_path / "chat.db")
    yield s
    s.close()


def _store(sink, ids):
    for i in ids:
        sink.save_chat_message({"id": i, "chatChannelId": "c", "content": ""}, [])
    sink.conn.commit()


def _ids(sink):
    return [r[0] for r in sink.conn.execute("SELECT id FROM chat_messages ORDER BY id")]


@pytest.mark.unit
def test_gap_fills_hole_then_stops(sink, monkeypatch):
    # stored 1..6 (old archive); the API now has 1..15 -> hole is 7..15
    _store(sink, range(1, 7))
    calls = []
    fake = _fake_api(list(range(1, 16)))
    monkeypatch.setattr(backfill, "fetch_page",
                        lambda *a, **k: calls.append(k.get("cursor")) or fake(*a, **k))
    backfill.backfill_channel("c", sink=sink, page_size=PAGE, delay=0, gap=True)
    assert _ids(sink) == list(range(1, 16))
    # pages 13-15, 10-12, 7-9, then 4-6 is fully stored -> stop (never asks for <4)
    assert calls == [None, 13, 10, 7]


@pytest.mark.unit
def test_gap_skips_stored_first_page(sink, monkeypatch):
    # a short earlier poll stored the newest page (13-15) on top of the old archive
    _store(sink, [1, 2, 3, 4, 5, 6, 13, 14, 15])
    monkeypatch.setattr(backfill, "fetch_page", _fake_api(list(range(1, 16))))
    backfill.backfill_channel("c", sink=sink, page_size=PAGE, delay=0, gap=True)
    assert _ids(sink) == list(range(1, 16))


@pytest.mark.unit
def test_resume_without_gap_walks_older(sink, monkeypatch):
    _store(sink, [10, 11, 12])
    monkeypatch.setattr(backfill, "fetch_page", _fake_api(list(range(1, 13))))
    backfill.backfill_channel("c", sink=sink, page_size=PAGE, delay=0)
    assert _ids(sink) == list(range(1, 13))
