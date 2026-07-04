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
import shutil
import sqlite3
import time
from pathlib import Path

import requests
from loguru import logger

from . import token_store
from .api_client import record

SUPABASE_BASE = "https://ajyrhjlbiyqtzxiubwhl.supabase.co/rest/v1/research_report"
SUPABASE_APIKEY = "sb_publishable_p16tYte2BDxxrAHa2mDl2Q_KhWPesCe"
FETCH_URL = "https://app.godelterminal.com/api/fetchResearch"
PAGE_SIZE = 100

PDF_DIR = Path(__file__).resolve().parent.parent / "output" / "research_pdfs"
DB_PATH = Path(__file__).resolve().parent.parent / "research.db"

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
CREATE INDEX IF NOT EXISTS idx_research_downloaded ON research_reports(downloaded);
CREATE INDEX IF NOT EXISTS idx_research_date ON research_reports(date);
CREATE INDEX IF NOT EXISTS idx_research_provider ON research_reports(provider);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
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


def sync_metadata(delay: float = 0.15, page_size: int = PAGE_SIZE) -> int:
    """Walk the full catalog and upsert metadata for every report. Cheap (JSON
    only, no PDFs) -- safe to re-run to pick up newly published reports."""
    conn = connect()
    n = 0
    try:
        offset = 0
        total = None
        while total is None or offset < total:
            rows, total = list_page(offset, page_size)
            if not rows:
                break
            upsert_metadata(conn, rows)
            n += len(rows)
            offset += len(rows)
            if n % 5000 < page_size:
                logger.info("research metadata sync: {}/{}", n, total)
            if offset < total:
                time.sleep(delay)
    finally:
        conn.close()
    return n


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


def list_page(offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """One page of report metadata (no login needed). Returns (rows, total_count)."""
    headers = {
        "apikey": SUPABASE_APIKEY,
        "Prefer": "count=exact",
        "Range": f"{offset}-{offset + limit - 1}",
    }
    params = {"select": "*", "order": "date.desc,id.desc"}
    t0 = time.time()
    resp = requests.get(SUPABASE_BASE, headers=headers, params=params, timeout=30)
    ms = (time.time() - t0) * 1000
    record("GET", "/rest/v1/research_report", {"range": headers["Range"]},
           resp.status_code, ms, host="godel-research")
    resp.raise_for_status()
    total = 0
    cr = resp.headers.get("content-range", "")  # "0-99/851762"
    if "/" in cr:
        total = int(cr.rsplit("/", 1)[1])
    return resp.json(), total


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


def fetch_pdf(report_id: int) -> bytes:
    """Download one report's PDF bytes."""
    token = token_store.load_token()
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://app.godelterminal.com",
        "Referer": "https://app.godelterminal.com/",
        "Cookie": f"__sessionv2={token}",
        # Cloudflare fronts app.godelterminal.com (unlike api.) and blocks
        # requests with no User-Agent as a 403, even with a valid session cookie.
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) "
                        "Gecko/20100101 Firefox/148.0"),
    }
    t0 = time.time()
    resp = requests.post(FETCH_URL, headers=headers, json={"id": report_id}, timeout=60)
    ms = (time.time() - t0) * 1000
    record("POST", "/api/fetchResearch", {"id": report_id}, resp.status_code, ms,
           host="godel-research")
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "pdf" not in ctype.lower():
        raise ValueError(f"expected a PDF, got content-type={ctype!r} "
                          f"(id={report_id}, {len(resp.content)} bytes)")
    return resp.content


def _safe_name(row: dict) -> str:
    title = (row.get("title") or "untitled").strip()
    title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:80]
    return f"{row['id']}_{title}.pdf"


def _shard_dir(row: dict, base: Path = PDF_DIR) -> Path:
    # 851k files in one flat dir is rough on Finder/Spotlight; shard by year.
    year = (row.get("date") or "unknown")[:4]
    return base / year


def download_one(row: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or _shard_dir(row)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _safe_name(row)
    pdf_bytes = fetch_pdf(row["id"])
    path.write_bytes(pdf_bytes)
    logger.info("saved {} ({} bytes)", path.name, len(pdf_bytes))
    return path


def pending_ids(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    q = "SELECT * FROM research_reports WHERE downloaded = 0 ORDER BY date DESC, id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    cols = [d[0] for d in conn.execute(q).description]
    return [dict(zip(cols, row)) for row in conn.execute(q).fetchall()]


def download_pending(limit: int | None = None, delay: float = 0.5,
                      min_free_gb: float = 5.0) -> dict:
    """Download every not-yet-downloaded report (resumable: already-downloaded
    rows are skipped on the next call since they're filtered by the
    `downloaded` flag). Stops itself -- instead of crashing mid-write -- once
    free disk space on PDF_DIR's volume drops below `min_free_gb`, so it never
    fills the disk out from under the live chat poller / godel_rest.db."""
    conn = connect()
    ok, failed = 0, 0
    stopped_low_disk = False
    try:
        rows = pending_ids(conn, limit)
        n = len(rows)
        for i, row in enumerate(rows, 1):
            free_gb = shutil.disk_usage(PDF_DIR.parent).free / 1e9
            if free_gb < min_free_gb:
                logger.warning("stopping: only {:.1f}GB free (< {}GB floor). "
                                "{}/{} attempted this run -- resume later, "
                                "already-downloaded rows are skipped.",
                                free_gb, min_free_gb, i - 1, n)
                stopped_low_disk = True
                break
            try:
                path = download_one(row)
                conn.execute(
                    "UPDATE research_reports SET downloaded=1, downloaded_at=?, "
                    "pdf_path=?, download_error=NULL WHERE id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     str(path), row["id"]),
                )
                ok += 1
            except Exception as e:
                logger.warning("failed id={}: {}", row["id"], e)
                conn.execute(
                    "UPDATE research_reports SET download_error=? WHERE id=?",
                    (str(e), row["id"]),
                )
                failed += 1
            conn.commit()
            if i % 50 == 0:
                logger.info("research backfill: {}/{} ({} ok, {} failed)",
                            i, n, ok, failed)
            time.sleep(delay)
    finally:
        conn.close()
    return {"attempted": ok + failed, "ok": ok, "failed": failed,
            "stopped_low_disk": stopped_low_disk}
