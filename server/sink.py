"""SQLite sink. Idempotent inserts keyed on each source's natural id so re-runs
and overlapping polls never duplicate rows. No row is ever mutated in place;
edits/deletes are recorded as new column values via INSERT OR REPLACE only when
the upstream record itself changed."""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "godel_rest.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id           INTEGER PRIMARY KEY,
    channel_id   TEXT,
    channel_type TEXT,
    created_at   TEXT,
    edited_at    TEXT,
    deleted_at   TEXT,
    user_id      TEXT,
    username     TEXT,
    is_admin     INTEGER,
    parent_id    INTEGER,
    parent_username TEXT,
    parent_content  TEXT,
    content      TEXT,
    reactions    TEXT,
    raw          TEXT,
    inserted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at);

CREATE TABLE IF NOT EXISTS chat_tickers (
    message_id INTEGER,
    ticker     TEXT,
    series_id  INTEGER,
    name       TEXT,
    figi       TEXT,
    isin       TEXT,
    cik        TEXT,
    series_type TEXT,
    PRIMARY KEY (message_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_ticker ON chat_tickers(ticker);

CREATE TABLE IF NOT EXISTS news_items (
    id          TEXT PRIMARY KEY,
    source      TEXT,
    type        TEXT,
    time        TEXT,
    important   INTEGER,
    content     TEXT,
    symbols     TEXT,
    raw         TEXT,
    inserted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_source ON news_items(source);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(time);
"""


class Sink:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after a db was first created."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(chat_messages)")}
        for name in ("parent_username", "parent_content"):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {name} TEXT")

    def close(self):
        self.conn.close()

    def save_chat_message(self, msg: dict, tickers: list[dict]) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        parent = msg.get("parentMessage") or {}
        self.conn.execute(
            """INSERT OR REPLACE INTO chat_messages
               (id, channel_id, channel_type, created_at, edited_at, deleted_at,
                user_id, username, is_admin, parent_id, parent_username,
                parent_content, content, reactions, raw, inserted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg["id"],
                msg.get("chatChannelId"),
                msg.get("chatChannelType"),
                msg.get("createdAt"),
                msg.get("editedAt"),
                msg.get("deletedAt"),
                msg.get("userId"),
                msg.get("username"),
                1 if msg.get("isUserAdmin") else 0,
                parent.get("id") if parent else None,
                parent.get("username") if parent else None,
                parent.get("content") if parent else None,
                msg.get("content"),
                json.dumps(msg.get("reactions") or []),
                json.dumps(msg),
                now,
            ),
        )
        for t in tickers:
            self.conn.execute(
                """INSERT OR REPLACE INTO chat_tickers
                   (message_id, ticker, series_id, name, figi, isin, cik, series_type)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    msg["id"],
                    t.get("ticker"),
                    t.get("series_id"),
                    t.get("name"),
                    t.get("figi"),
                    t.get("isin"),
                    t.get("cik"),
                    t.get("series_type"),
                ),
            )
        self.conn.commit()

    def chat_id_exists(self, msg_id: int) -> bool:
        cur = self.conn.execute("SELECT 1 FROM chat_messages WHERE id=?", (msg_id,))
        return cur.fetchone() is not None

    def max_chat_id(self, channel_id: str) -> int:
        cur = self.conn.execute(
            "SELECT MAX(id) FROM chat_messages WHERE channel_id=?", (channel_id,)
        )
        row = cur.fetchone()
        return row[0] or 0

    def min_chat_id(self, channel_id: str):
        """Oldest id stored for a channel, or None if none yet."""
        cur = self.conn.execute(
            "SELECT MIN(id) FROM chat_messages WHERE channel_id=?", (channel_id,)
        )
        return cur.fetchone()[0]

    def channel_count(self, channel_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE channel_id=?", (channel_id,)
        )
        return cur.fetchone()[0]

    def save_news_item(self, item: dict) -> bool:
        """Returns True if this is a new id (inserted), False if already seen."""
        nid = str(item["id"])
        cur = self.conn.execute("SELECT 1 FROM news_items WHERE id=?", (nid,))
        is_new = cur.fetchone() is None
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.conn.execute(
            """INSERT OR REPLACE INTO news_items
               (id, source, type, time, important, content, symbols, raw, inserted_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                nid,
                item.get("_source"),
                item.get("type"),
                item.get("time"),
                1 if item.get("important") else 0,
                item.get("_content"),
                json.dumps(item.get("_symbols") or []),
                json.dumps(item),
                now,
            ),
        )
        self.conn.commit()
        return is_new
