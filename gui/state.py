"""Read-only state for the GUI: service status, token expiry, archive stats,
recent messages and log tails. The page polls every few seconds, so anything
slow (COUNT(*) on ~1M rows, crontab, the channel list API) is cached."""
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from server import token_store
from server.sink import DB_PATH

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ROOT / "run"
SERVICES = ("token_server", "chat_poller", "backfill")
TOKEN_LIFETIME_S = 14 * 24 * 3600   # Gödel JWTs last 14 days
WARN_WINDOW_S = 24 * 3600           # matches server.token_watch
COUNT_TTL_S = 60
CRON_TTL_S = 30
CHANNELS_TTL_S = 24 * 3600
CHANNELS_RETRY_S = 300
CHANNELS_CACHE = token_store.STORE_DIR / "channels.json"
LOG_TAIL_BYTES = 64 * 1024
MAX_MESSAGES = 200


class TtlCache:
    """Remembers one computed value for `ttl` seconds."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._value = None
        self._at = 0.0

    def get(self, compute, now: float | None = None):
        now = time.time() if now is None else now
        if self._value is None or now - self._at >= self.ttl:
            self._value = compute()
            self._at = now
        return self._value


_count_cache = TtlCache(COUNT_TTL_S)
_cron_cache = TtlCache(CRON_TTL_S)


# ── services ────────────────────────────────────────────────────────────────
def service_pid(name: str, run_dir: Path = RUN_DIR) -> int | None:
    """PID from run/<name>.pid if that process is alive (same check as services.sh)."""
    try:
        pid = int((run_dir / f"{name}.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # alive, owned by another user
    return pid


def services(run_dir: Path = RUN_DIR) -> dict:
    out = {}
    for name in SERVICES:
        pid = service_pid(name, run_dir)
        out[name] = {"running": pid is not None, "pid": pid}
    return out


# ── token ───────────────────────────────────────────────────────────────────
def token_info(rec: dict | None, now: float) -> dict:
    if not rec:
        return {"captured": False, "state": "missing"}
    exp = rec.get("exp")
    secs = int(exp - now) if exp else None
    if secs is None or secs > WARN_WINDOW_S:
        state = "ok"
    elif secs <= 0:
        state = "expired"
    else:
        state = "warn"
    return {
        "captured": True,
        "exp": exp,
        "seconds_left": secs,
        "lifetime": TOKEN_LIFETIME_S,
        "captured_at": rec.get("captured_at"),
        "state": state,
    }


# ── expiry alerts (server.token_watch via cron) ─────────────────────────────
def _cron_has_token_watch() -> bool:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "server.token_watch" in r.stdout


def alerts_info() -> dict:
    state_path = token_store.STORE_DIR / "notify_state.json"
    try:
        last = json.loads(state_path.read_text()).get("last_notified_exp")
    except (OSError, ValueError):
        last = None
    return {"scheduled": _cron_cache.get(_cron_has_token_watch),
            "last_notified_exp": last}


# ── archive ─────────────────────────────────────────────────────────────────
def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Read-only use of the live DB; the timeout rides out writer commits."""
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=5)
    conn.execute("PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row
    return conn


def to_epoch_ms(created_at: str | None) -> int | None:
    """Gödel timestamps are UTC; newer ones end in Z, older ones have no zone."""
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def db_stats(conn: sqlite3.Connection, now: float) -> dict:
    newest = conn.execute("SELECT MAX(created_at) FROM chat_messages").fetchone()[0]
    last_hour = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?",
        (_utc_iso(now - 3600),),
    ).fetchone()[0]
    total = _count_cache.get(
        lambda: conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], now)
    return {"total": total, "newest_ms": to_epoch_ms(newest), "last_hour": last_hour}


def recent_messages(conn: sqlite3.Connection, titles: dict, limit: int = 80,
                    channel_id: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), MAX_MESSAGES))
    sql = ("SELECT id, channel_id, created_at, username, content, parent_username,"
           " parent_content FROM chat_messages")
    args: list = []
    if channel_id:
        sql += " WHERE channel_id = ?"
        args.append(channel_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [
        {
            "id": str(r["id"]),
            "channel_id": r["channel_id"],
            "channel": titles.get(r["channel_id"]) or (r["channel_id"] or "?")[:8],
            "created_ms": to_epoch_ms(r["created_at"]),
            "username": r["username"],
            "content": r["content"] or "",
            "parent_username": r["parent_username"],
            "parent_content": (r["parent_content"] or "")[:140],
        }
        for r in conn.execute(sql, args)
    ]


# ── channel titles (API, cached on disk) ────────────────────────────────────
class ChannelTitles:
    """id -> title map. Refreshed from the API at most daily; a failed fetch
    (e.g. expired token) keeps the last good copy and retries in 5 minutes."""

    def __init__(self, cache_path: Path = CHANNELS_CACHE, fetch=None):
        self.cache_path = cache_path
        self._fetch = fetch
        self._titles: dict = {}
        self._loaded_at = 0.0
        self._last_try = 0.0
        self._read_disk()

    def _read_disk(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        self._titles = data.get("titles", {})
        self._loaded_at = data.get("fetched_at", 0)

    def _default_fetch(self) -> list[dict]:
        from server import chat
        return chat.list_channels()

    def get(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        fresh = now - self._loaded_at < CHANNELS_TTL_S
        if fresh or now - self._last_try < CHANNELS_RETRY_S:
            return self._titles
        self._last_try = now
        try:
            chans = (self._fetch or self._default_fetch)()
        except Exception as e:  # network/token failure: keep the stale copy
            print(f"[gui] channel list refresh failed: {e}", flush=True)
            return self._titles
        self._titles = {c["id"]: c.get("title", "?") for c in chans if c.get("id")}
        self._loaded_at = now
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(
                {"fetched_at": now, "titles": self._titles}))
        except OSError as e:
            print(f"[gui] could not write channel cache: {e}", flush=True)
        return self._titles


# ── logs ────────────────────────────────────────────────────────────────────
def tail_log(name: str, lines: int = 80, run_dir: Path = RUN_DIR) -> list[str]:
    if name not in SERVICES:
        raise ValueError(f"unknown log {name!r}")
    path = run_dir / f"{name}.log"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
