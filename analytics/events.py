"""Build the `events` table: one row per (message, ticker) mention, enriched
with a direction label. This is the spine the skill/signal layers join against.

Incremental: only messages newer than the max message_id already in `events`
are processed on each run.
"""
import sqlite3

from .config import DB_PATH
from . import sentiment


def _backend(name: str):
    if name == "finbert":
        from . import sentiment_finbert
        return sentiment_finbert
    if name == "ollama":
        from . import sentiment_ollama
        return sentiment_ollama
    if name in ("openrouter", "kimi"):
        from . import sentiment_openrouter
        return sentiment_openrouter
    if name == "hybrid":
        from . import sentiment_hybrid
        return sentiment_hybrid
    return sentiment

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    message_id  INTEGER,
    ticker      TEXT,
    series_id   INTEGER,
    user_id     TEXT,
    username    TEXT,
    channel_id  TEXT,
    ts          TEXT,          -- message createdAt (UTC, no tz suffix)
    direction   INTEGER,       -- -1 / 0 / +1
    sent_score  REAL,          -- backend score of the message
    labeled_by  TEXT,          -- which backend produced `direction`
    PRIMARY KEY (message_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ticker ON events(ticker);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


META_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _migrate(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "labeled_by" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN labeled_by TEXT")
        conn.execute("UPDATE events SET labeled_by='lexicon' WHERE labeled_by IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_labeled "
                 "ON events(labeled_by)")
    conn.commit()


def build(classifier: str | None = None, since_days: int | None = None,
          log=print) -> int:
    """Two phases, both incremental and non-destructive:
      1. ensure every ticker-mention has an event row (labelled by the lexicon);
      2. if a stronger backend is requested, UPGRADE labels in place, chunk by
         chunk, so the analytics are never empty and a killed run resumes.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(EVENTS_SCHEMA)
    conn.executescript(META_SCHEMA)
    _migrate(conn)

    prev = conn.execute(
        "SELECT value FROM analytics_meta WHERE key='events_backend'").fetchone()
    if classifier is None:
        classifier = prev[0] if prev else "lexicon"

    # ── Phase 1: ensure rows exist (lexicon-labelled). Fast; keeps data live. ──
    new = conn.execute(
        """SELECT t.message_id, t.ticker, t.series_id, m.user_id, m.username,
                  m.channel_id, m.created_at, m.content
           FROM chat_tickers t
           JOIN chat_messages m ON m.id = t.message_id
           WHERE NOT EXISTS (SELECT 1 FROM events e
                             WHERE e.message_id = t.message_id
                               AND e.series_id = t.series_id)""").fetchall()
    inserted = 0
    if new:
        label_cache: dict[int, tuple] = {}
        batch = []
        for r in new:
            mid, content = r[0], r[7]
            if mid not in label_cache:
                label_cache[mid] = sentiment.classify(content)
            d, s = label_cache[mid]
            batch.append((r[0], r[1], r[2], r[3], r[4], r[5], r[6], d, s, "lexicon"))
        conn.executemany(
            """INSERT OR REPLACE INTO events
               (message_id, ticker, series_id, user_id, username, channel_id, ts,
                direction, sent_score, labeled_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", batch)
        conn.commit()
        inserted = len(batch)
        log(f"[events] phase 1: +{inserted} rows (lexicon)")

    # ── Phase 2: upgrade labels in place to the chosen backend. ──
    upgraded = 0
    if classifier != "lexicon":
        cutoff = None
        if since_days is not None:
            import datetime as _dt
            cutoff = (_dt.datetime.now(_dt.UTC)
                      - _dt.timedelta(days=since_days)).strftime("%Y-%m-%d")
        q = ("""SELECT DISTINCT e.message_id, m.created_at, m.content
                FROM events e JOIN chat_messages m ON m.id = e.message_id
                WHERE e.labeled_by != ?""")
        params = [classifier]
        if cutoff:
            q += " AND substr(e.ts,1,10) >= ?"
            params.append(cutoff)
        q += " ORDER BY e.message_id"
        targets = conn.execute(q, params).fetchall()

        if targets:
            backend = _backend(classifier)
            accepts_log = "log" in backend.classify_many.__code__.co_varnames
            uses_ollama = classifier == "ollama" or (
                classifier == "hybrid" and __import__("os").environ.get(
                    "HYBRID_LLM", "openrouter") == "ollama")
            if uses_ollama:
                from . import sentiment_ollama
                log("[events] warming up local LLM (loading model into memory)…")
                sentiment_ollama.warmup()
            chunk = 300
            log(f"[events] phase 2: upgrading {len(targets)} messages to "
                f"{classifier} (checkpoint every {chunk})…")
            for i in range(0, len(targets), chunk):
                part = targets[i:i + chunk]
                texts = [t[2] for t in part]
                labels = (backend.classify_many(texts, log=log) if accepts_log
                          else backend.classify_many(texts))
                conn.executemany(
                    "UPDATE events SET direction=?, sent_score=?, labeled_by=? "
                    "WHERE message_id=?",
                    [(d, s, classifier, part[j][0])
                     for j, (d, s) in enumerate(labels)])
                conn.commit()
                upgraded += len(part)
                log(f"[events] checkpoint {min(i + chunk, len(targets))}/"
                    f"{len(targets)} upgraded")

    conn.execute(
        "INSERT OR REPLACE INTO analytics_meta (key, value) VALUES ('events_backend', ?)",
        (classifier,))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    log(f"[events] done: +{inserted} new, {upgraded} upgraded, {total} total")
    return inserted + upgraded
