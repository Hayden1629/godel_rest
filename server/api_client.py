"""Thin wrapper around requests for the api.godelterminal.com host. Reads the
freshest token from token_store on every call so a browser-side refresh is
picked up without restarting the poller.

Every call is logged (loguru) to ~/.godel_rest/api.log and kept in an in-memory
ring buffer (`recent_calls()`) so the dashboard can show what's executing under
the hood: method, path, params, HTTP status, and elapsed milliseconds."""
import time
from collections import deque
from pathlib import Path

import requests
from loguru import logger

from . import token_store

BASE = "https://api.godelterminal.com"

# in-memory ring of the last N API calls, newest last — surfaced in the dashboard
_CALLS: deque = deque(maxlen=300)

_LOG_PATH = Path.home() / ".godel_rest" / "api.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
# Configure sinks once. API call logs go to the file only (not the console, so
# the CLI stays quiet); anything else keeps loguru's normal stderr behavior.
if not getattr(logger, "_godel_api_sink", False):
    import sys
    logger.remove()  # drop loguru's default stderr handler
    logger.add(sys.stderr, filter=lambda r: not r["extra"].get("godel_api"))
    logger.add(_LOG_PATH, rotation="5 MB", retention="7 days",
               filter=lambda r: r["extra"].get("godel_api"),
               format="{time:YYYY-MM-DD HH:mm:ss} | {level: <5} | {message}")
    logger._godel_api_sink = True  # type: ignore[attr-defined]

_log = logger.bind(godel_api=True)


def recent_calls(limit: int = 50) -> list[dict]:
    """The most recent API calls (newest last), for under-the-hood display."""
    return list(_CALLS)[-limit:]


def clear_calls() -> None:
    _CALLS.clear()


def log_path() -> str:
    return str(_LOG_PATH)


class TokenExpired(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: float = 5.0):
        self.retry_after = retry_after
        super().__init__(f"429 rate limited (retry after {retry_after}s)")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {token_store.load_token()}",
        "Origin": "https://app.godelterminal.com",
        "Referer": "https://app.godelterminal.com/",
        "Accept": "*/*",
    }


def record(method: str, path: str, params: dict | None,
           status: int | str, ms: float, host: str = "godel") -> None:
    """Append a call to the ring buffer + log it. Shared so other clients
    (e.g. the Massive client) can register their calls in the same view."""
    # strip secrets before they ever reach the buffer or the log file
    safe = {k: v for k, v in (params or {}).items() if k.lower() != "apikey"}
    entry = {"ts": time.strftime("%H:%M:%S"), "host": host, "method": method,
             "path": path, "params": safe, "status": status, "ms": round(ms, 1)}
    _CALLS.append(entry)
    # Any 2xx is a success. Ranged listing calls answer 206, and flagging those
    # as warnings marked every single catalog request with a ⚠, which hides the
    # ones that actually matter.
    ok = isinstance(status, int) and 200 <= status < 300
    level = "INFO" if ok else "WARNING"
    _log.log(level, "{} {} {} {} -> {} ({} ms){}", host, method, path,
             safe or "", status, round(ms, 1), "" if ok else " ⚠")


def _record(method: str, path: str, params: dict | None,
            status: int | str, ms: float) -> None:
    record(method, path, params, status, ms, host="godel")


def _check(r: requests.Response, path: str) -> None:
    """Raise the same token/rate/cloudflare errors get() does, shared by post()."""
    if r.status_code == 401:
        raise TokenExpired("401 from API — token rejected; relay a fresh one")
    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        raise RateLimited(float(ra) if ra and ra.isdigit() else 5.0)
    if r.status_code == 403:
        # Cloudflare would surface here; the api host should not challenge.
        raise RuntimeError(
            f"403 from API (path={path}). If this is a Cloudflare challenge "
            "the pure-poller assumption is broken; fall back to in-browser.")
    r.raise_for_status()


def get(path: str, params: dict | None = None, timeout: int = 15) -> dict:
    """GET an api.godelterminal.com path. `path` may be absolute or relative."""
    url = path if path.startswith("http") else f"{BASE}{path}"
    start = time.perf_counter()
    status: int | str = "ERR"
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        status = r.status_code
        _check(r, path)
        return r.json()
    finally:
        _record("GET", path, params, status, (time.perf_counter() - start) * 1000)


def post(path: str, body: dict, timeout: int = 30) -> dict:
    """POST a JSON body to an api.godelterminal.com path. `path` may be
    absolute or relative. Mirrors get()'s auth + error handling."""
    url = path if path.startswith("http") else f"{BASE}{path}"
    start = time.perf_counter()
    status: int | str = "ERR"
    try:
        r = requests.post(url, headers={**_headers(), "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        status = r.status_code
        _check(r, path)
        return r.json()
    finally:
        _record("POST", path, body, status, (time.perf_counter() - start) * 1000)
