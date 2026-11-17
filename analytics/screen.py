"""Bulk reverse-DCF screener across a universe of tickers (default: the S&P 500).

Runs the same model as ``analytics.run model TICKER`` for every name, but on a
lean path — no charts, no per-ticker file dump — so a few hundred tickers finish
in minutes. All three statements come from one cached financial-metric-group
fetch per ticker (see analytics.financials.fetch_metric_group).

Outputs a tidy row per ticker (valuation, reverse-DCF, latest growth/margins),
written to CSV and an interactive, self-contained HTML table you can sort and
filter in the browser.

    python3 -m analytics.screen                     # full S&P 500 -> output/
    python3 -m analytics.screen --limit 25          # quick smoke test
    python3 -m analytics.screen --workers 8 --out output/sp500_screen

CLI is also wired as `python3 -m analytics.run screen`.
"""
from __future__ import annotations

import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import financials as F
from . import model as M
from . import screen_html

# datasets.io keeps a current S&P 500 constituents CSV (Symbol, Security, GICS…)
SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv")
_CACHE_DIR = Path(__file__).resolve().parent / "data"
_SP500_CACHE = _CACHE_DIR / "sp500.csv"

# columns in output order: (key, label, kind) — kind drives HTML formatting/sort
# kinds: str | usd | usd_millions | mult | pct | num
COLUMNS: list[tuple[str, str, str]] = [
    ("ticker", "Ticker", "str"),
    ("name", "Name", "str"),
    ("sector", "Sector", "str"),
    ("price", "Price", "usd"),
    ("market_cap", "Mkt Cap", "usd"),
    ("ev", "EV", "usd"),
    ("fcf_ttm", "FCF (TTM)", "usd_millions"),
    ("ev_to_fcf", "EV/FCF", "mult"),
    ("implied_g", "Implied FCF g", "pct"),
    ("hist_cagr", "Hist FCF CAGR", "pct"),
    ("growth_gap", "Implied − Hist", "pct"),
    ("rev_yoy", "Rev YoY", "pct"),
    ("gross_m", "Gross margin", "pct"),
    ("net_m", "Net margin", "pct"),
]


# ── universe ──────────────────────────────────────────────────────────────────


def sp500_constituents(refresh: bool = False) -> list[dict]:
    """S&P 500 rows [{symbol, name, sector}], cached locally after first fetch."""
    if _SP500_CACHE.exists() and not refresh:
        text = _SP500_CACHE.read_text()
    else:
        r = requests.get(SP500_CSV_URL, headers={"User-Agent": "godel-rest"},
                         timeout=30)
        r.raise_for_status()
        text = r.text
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _SP500_CACHE.write_text(text)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append({
            "symbol": (row.get("Symbol") or "").strip(),
            "name": (row.get("Security") or "").strip(),
            "sector": (row.get("GICS Sector") or "").strip(),
        })
    return [r for r in rows if r["symbol"]]


def _load_tickers_file(path: str) -> list[dict]:
    """A plain-text or CSV file of tickers (one per line, or a Symbol column)."""
    text = Path(path).read_text()
    if "," in text.splitlines()[0]:
        return [{"symbol": (r.get("Symbol") or r.get("symbol") or "").strip(),
                 "name": (r.get("Security") or r.get("name") or "").strip(),
                 "sector": (r.get("GICS Sector") or r.get("sector") or "").strip()}
                for r in csv.DictReader(io.StringIO(text))]
    return [{"symbol": ln.strip(), "name": "", "sector": ""}
            for ln in text.splitlines() if ln.strip()]


# ── per-ticker model (lean: no charts, no file writes) ────────────────────────


def screen_one(ticker: str, period: str = "QTR") -> dict:
    """Valuation + reverse-DCF + latest growth/margins for one ticker.

    Reuses model.py's assembly but skips chart rendering and CSV/JSON dumps.
    Raises on network/resolution failure so the caller can record it."""
    sid = F.resolve_series_id(ticker)
    # all three statements resolve from one cached metric-group payload
    frames = {s: F.fetch_statement(sid, s, period) for s in F.STATEMENTS}
    mdf = M.build_model_frame(frames)
    val = M.build_valuation(ticker, mdf)
    rd = M.reverse_dcf(val, mdf)

    def latest(col: str):
        if col in mdf.columns:
            s = mdf[col].dropna()
            return float(s.iloc[-1]) if len(s) else None
        return None

    implied = rd.get("implied_fcf_growth")
    hist = rd.get("historical_fcf_cagr")
    gap = (implied - hist) if (implied is not None and hist is not None) else None
    return {
        "ticker": ticker,
        "price": val.price,
        "market_cap": val.market_cap,
        "ev": val.enterprise_value,
        "fcf_ttm": val.fcf_ttm,
        "ev_to_fcf": rd.get("ev_to_fcf"),
        "implied_g": implied,
        "hist_cagr": hist,
        "growth_gap": round(gap, 4) if gap is not None else None,
        "rev_yoy": latest("revenue_yoy"),
        "gross_m": latest("gross_margin"),
        "net_m": latest("net_margin"),
        "read": rd.get("read") or rd.get("error"),
    }


def _screen_with_retry(ticker: str, period: str, retries: int = 3) -> dict:
    """screen_one with backoff on rate limiting; token expiry aborts loudly."""
    from server.api_client import RateLimited, TokenExpired
    for attempt in range(retries + 1):
        try:
            return screen_one(ticker, period)
        except RateLimited as e:
            if attempt == retries:
                raise
            time.sleep(getattr(e, "retry_after", 5.0))
        except TokenExpired:
            raise  # no point continuing the whole run on a dead token
    raise RuntimeError("unreachable")


# ── orchestration ─────────────────────────────────────────────────────────────


def screen_universe(universe: list[dict], period: str = "QTR", workers: int = 6,
                    progress: bool = True) -> tuple[list[dict], list[dict]]:
    """Screen every ticker concurrently. Returns (rows, errors)."""
    meta = {u["symbol"].upper(): u for u in universe}
    rows: list[dict] = []
    errors: list[dict] = []
    done = 0
    total = len(universe)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_screen_with_retry, u["symbol"], period): u["symbol"]
                for u in universe}
        for fut in as_completed(futs):
            sym = futs[fut]
            done += 1
            try:
                row = fut.result()
                m = meta.get(sym.upper(), {})
                row["name"] = m.get("name") or row.get("name") or ""
                row["sector"] = m.get("sector") or ""
                rows.append(row)
            except Exception as e:  # network / resolution / data gap
                errors.append({"ticker": sym, "error": str(e)[:200]})
            if progress and (done % 25 == 0 or done == total):
                rate = done / max(time.time() - t0, 1e-6)
                print(f"  {done}/{total}  ({len(errors)} errors)  "
                      f"{rate:.1f}/s", flush=True)

    rows.sort(key=lambda r: (r.get("ev_to_fcf") is None, r.get("ev_to_fcf") or 0))
    return rows, errors


def write_csv(rows: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [k for k, _, _ in COLUMNS] + ["read"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run(universe_name: str = "S&P 500", period: str = "QTR", workers: int = 6,
        out: str = "output/sp500_screen", limit: int | None = None,
        tickers_file: str | None = None, refresh: bool = False) -> dict:
    """Full screen: build universe -> screen -> write CSV + interactive HTML."""
    if tickers_file:
        universe = _load_tickers_file(tickers_file)
        universe_name = Path(tickers_file).stem
    else:
        universe = sp500_constituents(refresh=refresh)
    if limit:
        universe = universe[:limit]

    print(f"screening {len(universe)} tickers ({universe_name}) "
          f"with {workers} workers…", flush=True)
    rows, errors = screen_universe(universe, period=period, workers=workers)

    out_base = Path(out)
    csv_path = out_base.with_suffix(".csv")
    html_path = out_base.with_suffix(".html")
    write_csv(rows, csv_path)
    screen_html.render(rows, errors, COLUMNS, html_path,
                       universe_name=universe_name, period=period)

    print(f"\n{len(rows)} screened, {len(errors)} errors")
    print(f"csv:  {csv_path}")
    print(f"html: {html_path}  (open in a browser to sort/filter)")
    return {"rows": rows, "errors": errors,
            "csv": str(csv_path), "html": str(html_path)}


def main():
    import argparse
    p = argparse.ArgumentParser(prog="analytics.screen")
    p.add_argument("--out", default="output/sp500_screen",
                   help="output path stem (writes .csv and .html)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--period", default="QTR", choices=["QTR", "ANN"])
    p.add_argument("--limit", type=int, default=None,
                   help="only the first N tickers (smoke test)")
    p.add_argument("--tickers-file", default=None,
                   help="custom universe: .txt (one ticker/line) or .csv")
    p.add_argument("--refresh", action="store_true",
                   help="re-download the S&P 500 constituent list")
    a = p.parse_args()
    run(period=a.period, workers=a.workers, out=a.out, limit=a.limit,
        tickers_file=a.tickers_file, refresh=a.refresh)


if __name__ == "__main__":
    main()
