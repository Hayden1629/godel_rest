"""GUI state helpers + loopback server guards. No network, no live DB.

    .venv/bin/python -m pytest tests/test_gui.py
"""
import http.client
import json
import os
import threading

import pytest

from gui import actions, server, state
from server.sink import Sink

NOW = 1_800_000_000.0


def _msg(mid: int, channel: str, created: str, content: str = "hi", **extra) -> dict:
    return {"id": mid, "chatChannelId": channel, "createdAt": created,
            "username": "alice", "content": content, **extra}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "chat.db"
    sink = Sink(db_path=path)
    sink.save_chat_message(_msg(1, "c1", "2026-07-01T19:21:59.50166"), [])
    sink.save_chat_message(_msg(2, "c2", "2027-01-15T07:59:00.000000Z", "[NVDA:4427080] up"), [])
    sink.save_chat_message(
        _msg(3, "c1", "2027-01-15T07:59:30.123456Z", "agreed",
             parentMessage={"id": 2, "username": "bob", "content": "x" * 300}), [])
    sink.conn.commit()
    sink.close()
    return path


# ── services ────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_service_pid_alive_dead_and_missing(tmp_path):
    (tmp_path / "chat_poller.pid").write_text(str(os.getpid()))
    (tmp_path / "backfill.pid").write_text("999999999")
    (tmp_path / "token_server.pid").write_text("not-a-pid")
    svcs = state.services(tmp_path)
    assert svcs["chat_poller"] == {"running": True, "pid": os.getpid()}
    assert svcs["backfill"] == {"running": False, "pid": None}
    assert svcs["token_server"] == {"running": False, "pid": None}


# ── token ───────────────────────────────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.parametrize("secs_left,expected", [
    (5 * 24 * 3600, "ok"),
    (3600, "warn"),
    (0, "expired"),
    (-60, "expired"),
])
def test_token_info_states(secs_left, expected):
    info = state.token_info({"exp": NOW + secs_left, "captured_at": NOW - 10}, NOW)
    assert info["state"] == expected
    assert info["seconds_left"] == secs_left


@pytest.mark.unit
def test_token_info_missing_and_no_exp():
    assert state.token_info(None, NOW) == {"captured": False, "state": "missing"}
    assert state.token_info({"exp": None}, NOW)["state"] == "ok"


# ── timestamps ──────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_to_epoch_ms_handles_both_api_formats():
    # old rows: no zone, 5 fractional digits; new rows: Z, 6 digits — both UTC
    assert state.to_epoch_ms("2026-07-01T19:21:59.50166") == 1782933719501
    assert state.to_epoch_ms("2026-07-01T19:21:59.501660Z") == 1782933719501
    assert state.to_epoch_ms(None) is None
    assert state.to_epoch_ms("garbage") is None


# ── archive ─────────────────────────────────────────────────────────────────
@pytest.mark.integration
def test_db_stats(db_path):
    conn = state.connect(db_path)
    stats = state.db_stats(conn, state.to_epoch_ms("2027-01-15T08:00:00Z") / 1000)
    conn.close()
    assert stats["newest_ms"] == state.to_epoch_ms("2027-01-15T07:59:30.123456Z")
    assert stats["last_hour"] == 2


@pytest.mark.integration
def test_recent_messages_order_filter_and_titles(db_path):
    conn = state.connect(db_path)
    msgs = state.recent_messages(conn, {"c1": "general"})
    only_c2 = state.recent_messages(conn, {}, channel_id="c2")
    conn.close()
    assert [m["id"] for m in msgs] == ["3", "2", "1"]
    assert msgs[0]["channel"] == "general"
    assert msgs[1]["channel"] == "c2"            # unknown id falls back to the id
    assert msgs[0]["parent_username"] == "bob"
    assert len(msgs[0]["parent_content"]) == 140
    assert [m["id"] for m in only_c2] == ["2"]


@pytest.mark.integration
def test_connect_is_read_only(db_path):
    conn = state.connect(db_path)
    with pytest.raises(Exception):
        conn.execute("DELETE FROM chat_messages")
    conn.close()


@pytest.mark.unit
def test_recent_messages_clamps_limit(db_path):
    conn = state.connect(db_path)
    assert len(state.recent_messages(conn, {}, limit=0)) == 1
    assert len(state.recent_messages(conn, {}, limit=10_000)) == 3
    conn.close()


# ── caches ──────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_ttl_cache_recomputes_after_ttl():
    calls = []
    cache = state.TtlCache(60)
    compute = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert cache.get(compute, now=100) == 1
    assert cache.get(compute, now=150) == 1
    assert cache.get(compute, now=161) == 2


@pytest.mark.unit
def test_channel_titles_fetch_cache_and_failure(tmp_path):
    cache = tmp_path / "channels.json"
    titles = state.ChannelTitles(cache, fetch=lambda: [{"id": "c1", "title": "general"}])
    assert titles.get(now=NOW) == {"c1": "general"}
    assert json.loads(cache.read_text())["titles"] == {"c1": "general"}

    def boom():
        raise RuntimeError("401")

    stale = state.ChannelTitles(cache, fetch=boom)
    later = NOW + state.CHANNELS_TTL_S + 1
    assert stale.get(now=later) == {"c1": "general"}   # keeps last good copy


# ── logs ────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_tail_log(tmp_path):
    (tmp_path / "backfill.log").write_text("\n".join(f"line {i}" for i in range(100)))
    assert state.tail_log("backfill", lines=3, run_dir=tmp_path) == ["line 97", "line 98", "line 99"]
    assert state.tail_log("chat_poller", run_dir=tmp_path) == []
    with pytest.raises(ValueError):
        state.tail_log("../../etc/passwd", run_dir=tmp_path)


# ── actions ─────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_unknown_action_never_runs():
    r = actions.run("rm -rf /")
    assert r["ok"] is False and "unknown action" in r["error"]


# ── server ──────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_static_file_blocks_traversal():
    assert server.static_file("/").name == "index.html"
    assert server.static_file("/static/app.js").name == "app.js"
    assert server.static_file("/static/../server.py") is None
    assert server.static_file("/../../server/cli.py") is None


@pytest.fixture
def live(db_path, monkeypatch, tmp_path):
    monkeypatch.setattr(state, "DB_PATH", db_path)
    monkeypatch.setattr(server.Handler, "titles",
                        state.ChannelTitles(tmp_path / "ch.json", fetch=lambda: []))
    srv = server.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def _request(port, method, path, host=None, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    conn.request(method, path, body=body, headers=hdrs)
    res = conn.getresponse()
    payload = json.loads(res.read())
    conn.close()
    return res.status, payload


@pytest.mark.integration
def test_server_messages_endpoint(live):
    status, body = _request(live, "GET", "/api/messages?limit=2")
    assert status == 200 and body["ok"] is True
    assert [m["id"] for m in body["data"]["messages"]] == ["3", "2"]


@pytest.mark.integration
def test_server_rejects_foreign_host(live):
    status, body = _request(live, "GET", "/api/state", host="evil.example:80")
    assert status == 403 and body["error"] == "bad host"


@pytest.mark.integration
def test_server_post_requires_gui_header(live):
    payload = json.dumps({"action": "start"})
    status, body = _request(live, "POST", "/api/action", body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 403 and body["error"] == "missing GUI header"


@pytest.mark.integration
def test_server_post_unknown_action(live):
    status, body = _request(live, "POST", "/api/action",
                            body=json.dumps({"action": "nope"}),
                            headers={"X-Godel-GUI": "1", "Content-Type": "application/json"})
    assert status == 400 and "unknown action" in body["error"]


@pytest.mark.integration
def test_server_bad_log_name(live):
    status, body = _request(live, "GET", "/api/log?name=../secret")
    assert status == 400 and body["ok"] is False
