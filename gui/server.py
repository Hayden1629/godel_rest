"""Loopback HTTP server behind the GUI window: serves gui/static/ plus a small
JSON API. Responses use one envelope: {"ok": bool, "data": ..., "error": str|None}.

Guards (any web page can aim requests at 127.0.0.1):
  * Host must be 127.0.0.1/localhost:<port> — blocks DNS-rebinding pages.
  * POSTs need the X-Godel-GUI header — a custom header forces a CORS preflight,
    which we never approve, so other origins can't trigger actions.
"""
import json
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from server import token_store

from . import actions, state, stats

HOST = "127.0.0.1"
PORT = 8766
STATIC_DIR = Path(__file__).resolve().parent / "static"
GUI_HEADER = "X-Godel-GUI"
MAX_BODY = 4096


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def static_file(url_path: str) -> Path | None:
    """Map a URL path to a file inside STATIC_DIR, or None (no traversal)."""
    rel = "index.html" if url_path in ("", "/") else url_path.lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    target = (STATIC_DIR / rel).resolve()
    if not target.is_relative_to(STATIC_DIR) or not target.is_file():
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    titles = state.ChannelTitles()
    stats_cache = stats.StatsCache()

    # ── plumbing ───────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data=None, error: str | None = None) -> None:
        payload = {"ok": error is None, "data": data, "error": error}
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in allowed_hosts(self.server.server_port)

    # ── routes ─────────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, error="bad host")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/state":
                return self._json(200, self._state())
            if url.path == "/api/messages":
                return self._json(200, self._messages(query))
            if url.path == "/api/stats":
                return self._json(200, self.stats_cache.overview(self.titles.get()))
            if url.path == "/api/daily":
                days = int(query.get("days", ["90"])[0])
                channel = query.get("channel", [""])[0] or None
                return self._json(200, {"days": days, "channel": channel,
                                        "rows": self.stats_cache.daily(days, channel)})
            if url.path == "/api/log":
                name = query.get("name", [""])[0]
                return self._json(200, {"name": name, "lines": state.tail_log(name)})
        except ValueError as e:
            return self._json(400, error=str(e))
        except Exception as e:
            print(f"[gui] {url.path} failed: {e!r}", flush=True)
            return self._json(500, error=f"{type(e).__name__}: {e}")
        target = static_file(url.path)
        if target is None:
            return self._json(404, error="not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, error="bad host")
        if self.headers.get(GUI_HEADER) != "1":
            return self._json(403, error="missing GUI header")
        if urlparse(self.path).path != "/api/action":
            return self._json(404, error="not found")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._json(413, error="body too large")
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, error="invalid JSON body")
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(action, str):
            return self._json(400, error="missing action")
        result = actions.run(action)
        code = 200 if result["ok"] else 400
        return self._json(code, {"output": result["output"]}, result["error"])

    # ── payloads ───────────────────────────────────────────────────────────
    def _state(self) -> dict:
        now = time.time()
        conn = state.connect()
        try:
            db = state.db_stats(conn, now)
        finally:
            conn.close()
        return {
            "now_ms": int(now * 1000),
            "services": state.services(),
            "token": state.token_info(token_store.load(), now),
            "alerts": state.alerts_info(),
            "db": db,
        }

    def _messages(self, query: dict) -> dict:
        limit = int(query.get("limit", ["80"])[0])
        channel = query.get("channel", [""])[0] or None
        titles = self.titles.get()
        conn = state.connect()
        try:
            msgs = state.recent_messages(conn, titles, limit=limit, channel_id=channel)
        finally:
            conn.close()
        return {"messages": msgs}

    def log_message(self, *args):
        pass  # the page polls constantly; keep the console quiet


def make_server(port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server
