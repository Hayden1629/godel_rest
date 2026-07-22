"""Godel's RES (research report) feed. Two decoupled pieces, mirroring chat/news:

- listing: a public Supabase PostgREST endpoint (`research_report` table) that
  needs no session, paginated 100 rows/page via the `Range` header. Returns
  metadata only (id, ticker, title, provider, date) -- no PDF bytes.
- download: `POST app.godelterminal.com/api/fetchResearch {"id": ...}` returns
  the raw PDF. Auth is the `__sessionv2` cookie, which is the same JWT family
  the extension already relays as a Bearer token (same iss/sub) -- so we reuse
  token_store's captured token as that cookie value, no extension changes.

Both endpoints were reverse-engineered from HARs in examples/reseach/.

Metadata + download state lives in its own SQLite file (research.db, separate
from godel_rest.db) so the ~850k-row catalog and its `downloaded` flag can be
tracked independently of the chat archive.
"""
import json
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import requests
from loguru import logger

from . import token_store
from .api_client import record

SUPABASE_BASE = "https://ajyrhjlbiyqtzxiubwhl.supabase.co/rest/v1/research_report"
SUPABASE_APIKEY = "sb_publishable_p16tYte2BDxxrAHa2mDl2Q_KhWPesCe"
FETCH_URL = "https://app.godelterminal.com/api/fetchResearch"
# PostgREST caps responses at 1000 rows (asking for more still returns 1000),
# so 1000 is the largest useful page and cuts a full catalog sweep from 8,787
# requests to 879.
PAGE_SIZE = 1000

_REPO = Path(__file__).resolve().parent.parent

# Where the PDFs land. Overridable so the ~800k-file corpus can live on a big
# external volume (e.g. GODEL_RESEARCH_DIR=/media/richard/GAMES/godel_research)
# while the repo stays on the system disk.
PDF_DIR = Path(os.environ.get("GODEL_RESEARCH_DIR")
               or _REPO / "output" / "research_pdfs").expanduser()
# The catalog DB deliberately defaults to the repo (a real local filesystem).
# Keep it off NTFS/exFAT/network mounts -- SQLite's locking is unreliable on
# FUSE-backed volumes, and this DB takes a write per completed download.
DB_PATH = Path(os.environ.get("GODEL_RESEARCH_DB") or _REPO / "research.db").expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_reports (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT,
    updated_at     TEXT,
    ticker         TEXT,      -- JSON array, as returned by the API
    title          TEXT,
    provider       TEXT,
    date           TEXT,
    region         TEXT,
    sector         TEXT,
    qa             TEXT,
    gics           TEXT,
    filter_concatenated TEXT,
    alt_id         INTEGER,
    downloaded     INTEGER NOT NULL DEFAULT 0,
    downloaded_at  TEXT,
    pdf_path       TEXT,
    download_error TEXT,
    inserted_at    TEXT
);
-- also serves iter_pending's keyset page (WHERE downloaded=0 AND id<? ORDER BY
-- id DESC): `id` is INTEGER PRIMARY KEY, i.e. the rowid, so this index's
-- entries are already ordered by it within each `downloaded` value and the
-- range scan comes free. A separate (downloaded, id) index adds nothing.
CREATE INDEX IF NOT EXISTS idx_research_downloaded ON research_reports(downloaded);
CREATE INDEX IF NOT EXISTS idx_research_date ON research_reports(date);
CREATE INDEX IF NOT EXISTS idx_research_provider ON research_reports(provider);
"""


BUSY_TIMEOUT_MS = 30_000


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    # WAL lets the backfill's streaming reader (iter_pending) run without
    # blocking against the writer; NORMAL sync is the right trade here since a
    # lost final commit only means re-downloading a few reports we can re-fetch.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # SQLite still allows only one writer at a time. Without a busy timeout,
    # a second writer (a `research-sync` run refreshing the catalog while a
    # backfill is going) fails instantly with "database is locked" instead of
    # waiting out the other transaction, which is measured in milliseconds.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA)
    return conn


def _scalar(v):
    """The API returns region/sector/QA/GICS as either a plain string or a list
    (e.g. region: ["India"]) -- JSON-encode anything non-scalar for storage."""
    return json.dumps(v) if isinstance(v, (list, dict)) else v


def upsert_metadata(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Insert/refresh metadata columns only -- never touches downloaded/pdf_path,
    so re-syncing (e.g. to pick up new reports) can't clobber download state."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.executemany(
        """INSERT INTO research_reports
             (id, created_at, updated_at, ticker, title, provider, date, region,
              sector, qa, gics, filter_concatenated, alt_id, inserted_at)
           VALUES (:id, :created_at, :updated_at, :ticker, :title, :provider, :date,
                   :region, :sector, :qa, :gics, :filter_concatenated, :alt_id, :inserted_at)
           ON CONFLICT(id) DO UPDATE SET
             created_at=excluded.created_at, updated_at=excluded.updated_at,
             ticker=excluded.ticker, title=excluded.title, provider=excluded.provider,
             date=excluded.date, region=excluded.region, sector=excluded.sector,
             qa=excluded.qa, gics=excluded.gics,
             filter_concatenated=excluded.filter_concatenated, alt_id=excluded.alt_id
        """,
        [{"inserted_at": now, "id": r.get("id"), "created_at": r.get("created_at"),
          "updated_at": r.get("updated_at"), "ticker": json.dumps(r.get("ticker") or []),
          "title": r.get("title"), "provider": r.get("provider"), "date": r.get("date"),
          "region": _scalar(r.get("region")), "sector": _scalar(r.get("sector")),
          "qa": _scalar(r.get("QA")), "gics": _scalar(r.get("GICS")),
          "filter_concatenated": r.get("filter_concatenated"), "alt_id": r.get("altID")}
         for r in rows],
    )
    conn.commit()


def sync_metadata(delay: float = 0.0, page_size: int = PAGE_SIZE,
                   workers: int = 4) -> int:
    """Walk the full catalog and upsert metadata for every report. Cheap (JSON
    only, no PDFs) -- safe to re-run to pick up newly published reports.

    Pages are independent, so they're fetched concurrently. Unlike the PDF
    endpoint (which plateaus around 7 req/s because the server is generating
    documents), this is a plain indexed query and scales cleanly to ~20k
    rows/s at 4 workers -- a full sweep is about a minute.

    Upserts happen on this thread so SQLite stays single-writer. Offsets are
    read concurrently against a table that may be taking inserts, so a row can
    shift pages and be missed; upserts are idempotent and re-running is cheap,
    which is the intended remedy (the old sequential version had this same
    property)."""
    conn = connect()
    n = 0
    try:
        _, total = list_page(0, 1)
        offsets = list(range(0, total, page_size))
        logger.info("research metadata sync: {} rows in {} pages, {} workers",
                    total, len(offsets), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Bounded batches rather than one big pool.map: mapping all 879
            # pages at once buffers every result in memory if the writer lags.
            for i in range(0, len(offsets), workers * 2):
                group = offsets[i:i + workers * 2]
                for rows in pool.map(lambda o: _fetch_page(o, page_size, delay), group):
                    if rows:
                        upsert_metadata(conn, rows)
                        n += len(rows)
                if n % 50_000 < page_size * workers * 2:
                    logger.info("research metadata sync: {}/{}", n, total)
    finally:
        conn.close()
    return n


def _fetch_page(offset: int, page_size: int, delay: float = 0.0) -> list[dict]:
    if delay:
        time.sleep(delay)
    rows, _ = list_page(offset, page_size)
    return rows


def counts() -> dict:
    conn = connect()
    try:
        total, downloaded, errored = conn.execute(
            "SELECT COUNT(*), SUM(downloaded), SUM(download_error IS NOT NULL) "
            "FROM research_reports"
        ).fetchone()
        return {"total": total or 0, "downloaded": downloaded or 0,
                "errored": errored or 0,
                "pending": (total or 0) - (downloaded or 0)}
    finally:
        conn.close()


<<<<<<< Updated upstream
def _get_with_retry(headers: dict, params: dict, ctx: dict,
                    retries: int = 5, backoff: float = 2.0) -> requests.Response:
    """GET with retry on transient network errors (dropped SSL reads, timeouts,
    5xx). Backs off exponentially. Raises the last error if all attempts fail."""
    last_exc = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            resp = requests.get(SUPABASE_BASE, headers=headers, params=params,
                                timeout=30)
            ms = (time.time() - t0) * 1000
            record("GET", "/rest/v1/research_report", ctx, resp.status_code, ms,
                   host="godel-research")
            if resp.status_code >= 500:
                raise requests.HTTPError(f"server {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ConnectionError) as e:
            last_exc = e
            if attempt == retries - 1:
                break
            wait = backoff * (2 ** attempt)
            logger.warning("request failed ({}); retry {}/{} in {:.0f}s",
                           e, attempt + 1, retries - 1, wait)
            time.sleep(wait)
    raise last_exc


def list_page(offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """One page of report metadata (no login needed). Returns (rows, total_count)."""
=======
def list_page(offset: int = 0, limit: int = PAGE_SIZE,
              retries: int = 5) -> tuple[list[dict], int]:
    """One page of report metadata (no login needed). Returns (rows, total_count).

    Retries transient failures. A full sweep is ~879 requests and a single
    unhandled `ConnectionResetError` used to abort the entire sync -- which is
    exactly how a real run died at 141k/878k rows."""
>>>>>>> Stashed changes
    headers = {
        "apikey": SUPABASE_APIKEY,
        "Prefer": "count=exact",
        "Range": f"{offset}-{offset + limit - 1}",
    }
    params = {"select": "*", "order": "date.desc,id.desc"}
<<<<<<< Updated upstream
    resp = _get_with_retry(headers, params, {"range": headers["Range"]})
    total = 0
    cr = resp.headers.get("content-range", "")  # "0-99/851762"
    if "/" in cr:
        total = int(cr.rsplit("/", 1)[1])
    return resp.json(), total
=======
    last: Exception | None = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            resp = _session().get(SUPABASE_BASE, headers=headers, params=params,
                                  timeout=60)
        except requests.RequestException as e:
            # A reset connection poisons the pooled socket; drop the Session so
            # the next attempt dials a fresh one.
            _local.session = None
            last = e
            record("GET", "/rest/v1/research_report", {"range": headers["Range"]},
                   "ERR", (time.time() - t0) * 1000, host="godel-research")
            time.sleep(min(2 ** attempt, 30))
            continue
        ms = (time.time() - t0) * 1000
        record("GET", "/rest/v1/research_report", {"range": headers["Range"]},
               resp.status_code, ms, host="godel-research")
        if resp.status_code == 429 or resp.status_code >= 500:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(2 ** attempt, 30)
            last = RuntimeError(f"HTTP {resp.status_code} listing offset={offset}")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        total = 0
        cr = resp.headers.get("content-range", "")  # "0-999/878686"
        if "/" in cr:
            total = int(cr.rsplit("/", 1)[1])
        return resp.json(), total
    raise last or RuntimeError(f"listing failed after {retries} attempts (offset={offset})")
>>>>>>> Stashed changes


def get_by_id(report_id: int) -> dict | None:
    """Metadata for one report (title/provider/date), for naming the saved PDF."""
    headers = {"apikey": SUPABASE_APIKEY}
    params = {"select": "*", "id": f"eq.{report_id}"}
    t0 = time.time()
    resp = requests.get(SUPABASE_BASE, headers=headers, params=params, timeout=30)
    ms = (time.time() - t0) * 1000
    record("GET", "/rest/v1/research_report", {"id": report_id}, resp.status_code, ms,
           host="godel-research")
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def iter_all(page_size: int = PAGE_SIZE, delay: float = 0.2):
    """Yield every report row, oldest-safe pagination (walks offset forward)."""
    offset = 0
    total = None
    while total is None or offset < total:
        rows, total = list_page(offset, page_size)
        if not rows:
            break
        yield from rows
        offset += len(rows)
        if offset < total:
            time.sleep(delay)


# --- request pacing -------------------------------------------------------
# Downloads run concurrently, so the server's own signals -- not a fixed sleep
# -- are what keep us in bounds. A 429/503 from any worker parks *every* worker
# until `_resume_at`, so the pool backs off as one instead of each thread
# discovering the limit separately.
_throttle_lock = threading.Lock()
_resume_at = 0.0
_local = threading.local()


def _session() -> requests.Session:
    """One Session per worker thread: keep-alive means we pay the TLS handshake
    once per thread instead of once per report, which is a large share of the
    per-request cost at these volumes."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


def _await_throttle() -> None:
    while True:
        with _throttle_lock:
            wait = _resume_at - time.monotonic()
        if wait <= 0:
            return
        time.sleep(min(wait, 5.0))


def _throttle(seconds: float) -> None:
    global _resume_at
    with _throttle_lock:
        _resume_at = max(_resume_at, time.monotonic() + seconds)


def _headers(token: str) -> dict:
    return {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://app.godelterminal.com",
        "Referer": "https://app.godelterminal.com/",
        "Cookie": f"__sessionv2={token}",
        # Cloudflare fronts app.godelterminal.com (unlike api.) and blocks
        # requests with no User-Agent as a 403, even with a valid session cookie.
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:148.0) "
                        "Gecko/20100101 Firefox/148.0"),
    }


def fetch_pdf(report_id: int, retries: int = 4) -> bytes:
    """Download one report's PDF bytes, retrying through rate limits.

    A 429 or 5xx is retried with exponential backoff (honouring `Retry-After`
    when the server sends one) rather than burning the report as a permanent
    error -- at 800k downloads, transient failures are certain."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        _await_throttle()
        token = token_store.load_token()
        t0 = time.time()
        try:
            resp = _session().post(FETCH_URL, headers=_headers(token),
                                   json={"id": report_id}, timeout=60)
        except requests.RequestException as e:
            last_exc = e
            record("POST", "/api/fetchResearch", {"id": report_id}, "ERR",
                   (time.time() - t0) * 1000, host="godel-research")
            time.sleep(2 ** attempt)
            continue
        ms = (time.time() - t0) * 1000
        record("POST", "/api/fetchResearch", {"id": report_id}, resp.status_code, ms,
               host="godel-research")

        if resp.status_code in (429, 503):
            ra = resp.headers.get("Retry-After")
            delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 2 ** (attempt + 2)
            logger.warning("rate limited (HTTP {}), pausing all workers {:.0f}s",
                           resp.status_code, delay)
            _throttle(delay)
            last_exc = RuntimeError(f"HTTP {resp.status_code} rate limited")
            continue
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"HTTP {resp.status_code} from fetchResearch")
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower():
            raise ValueError(f"expected a PDF, got content-type={ctype!r} "
                              f"(id={report_id}, {len(resp.content)} bytes)")
        return resp.content
    raise last_exc or RuntimeError(f"failed after {retries} attempts (id={report_id})")


def _safe_name(row: dict) -> str:
    title = (row.get("title") or "untitled").strip()
    title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:80]
    # NTFS rejects names whose final character is a space or a dot, which a
    # truncated title can easily produce -- strip before appending the suffix.
    title = title.rstrip(" .") or "untitled"
    return f"{row['id']}_{title}.pdf"


def _shard_dir(row: dict, base: Path | None = None) -> Path:
    # The catalog spans 1982-2026 and recent years hold 50k+ reports each, so
    # year-only shards still produce directories big enough to make listing
    # them painful (especially over NTFS/FUSE). Shard by year/month instead --
    # ~4-8k files per directory. Lookups go through research.db.pdf_path
    # anyway, so the layout only has to be pleasant to browse.
    base = base if base is not None else PDF_DIR
    date = row.get("date") or ""
    year, month = (date[:4] or "unknown"), (date[5:7] or "00")
    if not year.isdigit():
        return base / "unknown"
    return base / year / month


def download_one(row: dict, out_dir: Path | None = None, skip_existing: bool = True) -> Path:
    out_dir = out_dir or _shard_dir(row)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _safe_name(row)
    # A run killed mid-flight (or a DB restored from an older copy) can leave
    # files on disk the catalog doesn't know about -- don't re-pay for them.
    if skip_existing and path.exists() and path.stat().st_size > 0:
        return path
    pdf_bytes = fetch_pdf(row["id"])
    # Write to a temp name and rename: a kill -9 mid-write then leaves no
    # zero-length file that skip_existing would later mistake for a real one.
    tmp = path.with_suffix(".pdf.part")
    tmp.write_bytes(pdf_bytes)
    tmp.replace(path)
    logger.debug("saved {} ({} bytes)", path.name, len(pdf_bytes))
    return path


def iter_pending(limit: int | None = None, chunk: int = 2000):
    """Stream pending rows, one short-lived read transaction per chunk.

    `SELECT *` over ~880k pending rows would materialize about a gigabyte of
    dicts, so this pages. It pages by *keyset* rather than holding one cursor
    open, because a cursor left open for a 50-hour backfill is a read
    transaction left open for 50 hours -- and in WAL mode nothing can
    checkpoint past the oldest live reader, so the -wal file would grow to
    hold every write of the entire run. Opening and closing per chunk keeps
    the WAL recycling normally.

    Paging descends by id (not date, which is nullable and would silently
    strand rows in a row-value comparison). ids are assigned in ingest order,
    so this is still approximately newest-first, and rows the backfill marks
    downloaded simply drop out of later pages."""
    yielded = 0
    after: int | None = None
    while True:
        read = sqlite3.connect(str(DB_PATH))
        read.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            if after is None:
                cur = read.execute(
                    "SELECT * FROM research_reports WHERE downloaded = 0 "
                    "ORDER BY id DESC LIMIT ?", (chunk,))
            else:
                cur = read.execute(
                    "SELECT * FROM research_reports WHERE downloaded = 0 "
                    "AND id < ? ORDER BY id DESC LIMIT ?", (after, chunk))
            cols = [d[0] for d in cur.description]
            batch = cur.fetchall()
        finally:
            read.close()
        if not batch:
            return
        for row in batch:
            d = dict(zip(cols, row))
            after = d["id"]
            yield d
            yielded += 1
            if limit and yielded >= limit:
                return


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM research_reports WHERE downloaded = 0").fetchone()[0]


def _free_gb() -> float:
    """Free space on the volume PDF_DIR lives on. Walks up to the nearest
    existing ancestor so this works before the target tree has been created."""
    p = PDF_DIR
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def download_pending(limit: int | None = None, delay: float = 0.0,
                      min_free_gb: float = 5.0, workers: int = 8) -> dict:
    """Download every not-yet-downloaded report (resumable: already-downloaded
    rows are skipped on the next call since they're filtered by the
    `downloaded` flag). Stops itself -- instead of crashing mid-write -- once
    free disk space on PDF_DIR's volume drops below `min_free_gb`, so it never
    fills the disk out from under the live chat poller / godel_rest.db.

    `workers` threads fetch in parallel while this thread owns every DB write,
    which keeps SQLite single-writer. Throughput is governed by the server's
    429s (see `_throttle`) rather than a fixed per-request `delay`; `delay` is
    kept as an optional extra pause per download for deliberate gentleness."""
    conn = connect()
    ok, failed, done = 0, 0, 0
    stopped_low_disk = False
    started = time.time()
    try:
        n = pending_count(conn)
        if limit:
            n = min(n, limit)
        rows = iter_pending(limit)
        logger.info("research backfill: {} pending, {} workers, {:.0f}GB free at {}",
                    n, workers, _free_gb(), PDF_DIR)

        def work(row: dict):
            """Never raises: the row travels with its own outcome so the
            committer always knows which report to mark."""
            if delay:
                time.sleep(delay)
            try:
                return row, download_one(row), None
            except Exception as e:
                return row, None, e

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Keep only ~2x workers in flight: submitting all 800k futures up
            # front would hold the whole catalog in memory and make the
            # low-disk check unable to stop anything already queued.
            futures = set()
            for row in rows:
                futures.add(pool.submit(work, row))
                if len(futures) < workers * 2:
                    continue
                done_set, futures = _drain(futures)
                o, f = _commit(conn, done_set)
                ok, failed, done = ok + o, failed + f, done + o + f
                # statvfs on a FUSE mount isn't free, and this loop runs ~880k
                # times -- one reading per cycle, shared by the log and the guard.
                free = _free_gb()
                if done % 200 < len(done_set):
                    rate = done / max(time.time() - started, 1)
                    logger.info("research backfill: {}/{} ({} ok, {} failed) "
                                "{:.1f}/s, {:.0f}GB free", done, n, ok, failed,
                                rate, free)
                if free < min_free_gb:
                    logger.warning("stopping: only {:.1f}GB free (< {}GB floor). "
                                    "{}/{} done this run -- resume later, "
                                    "already-downloaded rows are skipped.",
                                    free, min_free_gb, done, n)
                    stopped_low_disk = True
                    break
            # drain whatever is still in flight
            for fut in futures:
                o, f = _commit(conn, [fut])
                ok, failed, done = ok + o, failed + f, done + o + f
    finally:
        conn.close()
    return {"attempted": ok + failed, "ok": ok, "failed": failed,
            "stopped_low_disk": stopped_low_disk}


def _drain(futures: set):
    """Block until at least one future finishes; return (finished, still_running)."""
    finished, pending = wait(futures, return_when=FIRST_COMPLETED)
    return list(finished), set(pending)


def _commit(conn: sqlite3.Connection, futures) -> tuple[int, int]:
    """Record finished downloads. Runs on the main thread only -- SQLite stays
    single-writer, so no `database is locked` under concurrency."""
    ok = failed = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for fut in futures:
        row, path, err = fut.result()
        if err is None:
            conn.execute(
                "UPDATE research_reports SET downloaded=1, downloaded_at=?, "
                "pdf_path=?, download_error=NULL WHERE id=?",
                (now, str(path), row["id"]),
            )
            ok += 1
        else:
            logger.warning("failed id={}: {}", row["id"], err)
            conn.execute("UPDATE research_reports SET download_error=? WHERE id=?",
                         (str(err), row["id"]))
            failed += 1
    conn.commit()
    return ok, failed
