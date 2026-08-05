"""Programmatic equivalents of the Gödel Terminal command mnemonics.

Each public function replicates what a terminal command (e.g. ``BBY EQ DES``)
fetches under the hood, returning a normalized dict/list you can print or feed
downstream. Endpoints were reverse-engineered from the live app; the ones that
changed (financials) are handled in ``analytics.financials``.

Discovered & verified (Aug 2026):
  DES   -> GET  /api/v2/company-profile/{seriesId}
  FA    -> analytics.financials (new financial-metric-group endpoint)
  EM    -> GET  /api/v1/earnings?seriesId=            (eps est/actual/surprise)
  ERN   -> GET  /api/v1/earnings-estimates?seriesId=
  SI    -> POST /api/v1/shortinterest {seriesId}
  ANR   -> GET  /api/v1/analyst-ratings?seriesId=
  TREND -> GET  /api/v1/trending?timeframe=
  MOST  -> GET  /api/most-v2
  IPO   -> GET  /api/ipos

Not yet discovered (return a NotDiscovered marker so the CLI can tell the user
to capture them with the endpoint monitor): TAS, HALT, TRAN.
"""
from __future__ import annotations

from . import api_client

# ── ticker resolution ─────────────────────────────────────────────────────────


def resolve_series_id(ticker: str) -> int:
    """Ticker -> primary series id via the search endpoint (exact match first)."""
    res = api_client.get("/api/v1/search", params={"query": ticker})
    insts = res.get("instruments") or []
    for inst in insts:
        if (inst.get("ticker") or "").upper() == ticker.upper():
            return inst["primary_series_id"]
    if insts:
        return insts[0]["primary_series_id"]
    raise ValueError(f"no instrument found for {ticker!r}")


class NotDiscovered(dict):
    """Marker for a command whose live endpoint hasn't been captured yet."""

    def __init__(self, command: str, hint: str):
        super().__init__(command=command, discovered=False, hint=hint)


# ── DES: company description / overview ───────────────────────────────────────

_DES_FIELDS = (
    "ticker", "name", "sector", "industry", "employees", "ceo", "website",
    "country", "currency", "marketCapStatic", "enterpriseValue", "trailingPe",
    "forwardPe", "pegRatio", "priceToSalesTtm", "priceToBookMrq", "beta",
    "sharesOutstanding", "floatShares", "percentHeldByInstitutions",
    "forwardAnnualDividendYield", "payoutRatio", "exDividendDate",
)


def des(ticker: str) -> dict:
    """Company overview — the DES panel fields."""
    sid = resolve_series_id(ticker)
    prof = api_client.get(f"/api/v2/company-profile/{sid}")
    out = {"series_id": sid, **{k: prof.get(k) for k in _DES_FIELDS}}
    out["description"] = prof.get("description")
    return out


# ── EM: earnings matrix (reported EPS vs estimate, surprise) ──────────────────


def em(ticker: str, limit: int = 12) -> dict:
    """Historical reported EPS vs. estimate and surprise, newest first."""
    sid = resolve_series_id(ticker)
    payload = api_client.get("/api/v1/earnings", params={"seriesId": sid})
    rows = payload.get("earnings") or []
    rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:limit]
    return {"series_id": sid, "meta": payload.get("meta") or {}, "earnings": rows}


# ── ERN: forward earnings estimates ───────────────────────────────────────────


def ern(ticker: str) -> dict:
    """Forward EPS estimates (per period, analyst count, low/high)."""
    sid = resolve_series_id(ticker)
    payload = api_client.get("/api/v1/earnings-estimates", params={"seriesId": sid})
    return {"series_id": sid, "meta": payload.get("meta") or {},
            "estimates": payload.get("earnings_estimate") or []}


# ── SI: short interest ────────────────────────────────────────────────────────

_SI_SCALARS = (
    "shortInterest", "shortInterestChange", "shortInterestRatio",
    "shortInterestRatioChange", "averageDailyVolume", "shortInterestHigh",
    "shortInterestLow",
)


def si(ticker: str) -> dict:
    """Latest short interest, days-to-cover ratio, and their changes.

    The endpoint also returns full time series (short interest & closing price);
    they're dropped here — pass ``raw=True`` to the CLI for the whole payload."""
    sid = resolve_series_id(ticker)
    payload = api_client.post("/api/v1/shortinterest", {"seriesId": sid})
    return {"series_id": sid, **{k: payload.get(k) for k in _SI_SCALARS},
            "raw": payload}


# ── ANR: analyst ratings ──────────────────────────────────────────────────────


def anr(ticker: str, limit: int = 20) -> dict:
    """Recent analyst rating actions (firm, action, current/prior), newest first."""
    sid = resolve_series_id(ticker)
    payload = api_client.get("/api/v1/analyst-ratings", params={"seriesId": sid})
    rows = payload.get("ratings") or []
    rows = sorted(rows, key=lambda r: (r.get("date") or "", r.get("time") or ""),
                  reverse=True)[:limit]
    return {"series_id": sid, "meta": payload.get("meta") or {}, "ratings": rows}


# ── TREND: trending tickers on Gödel ──────────────────────────────────────────


def trend(timeframe: str = "24H") -> dict:
    """Trending tickers ranked by mention count over the timeframe."""
    rows = api_client.get("/api/v1/trending", params={"timeframe": timeframe})
    out = []
    for r in rows or []:
        ctx = (r.get("seriesContext") or {}).get("instrument") or {}
        out.append({
            "ticker": r.get("ticker"),
            "total": r.get("totalCount"),
            "series_id": r.get("seriesId"),
            "name": ctx.get("name"),
        })
    out.sort(key=lambda x: x["total"] or 0, reverse=True)
    return {"timeframe": timeframe, "trending": out}


# ── MOST: most active securities ──────────────────────────────────────────────


def most(tab: str = "ACTIVE", rows: int = 25, sector: str = "All",
         min_market_cap: str = "UNLIMITED",
         max_market_cap: str = "UNLIMITED") -> dict:
    """Most active / gainers / losers. ``tab`` is ACTIVE|GAINERS|LOSERS."""
    payload = api_client.get("/api/most-v2", params={
        "tabType": tab, "rows": rows, "sector": sector,
        "minMarketCap": min_market_cap, "maxMarketCap": max_market_cap})
    out = []
    for s in payload.get("securities") or []:
        sym = (((s.get("seriesContext") or {}).get("activeSymbolListing")
                or {}).get("symbol") or {})
        out.append({
            "ticker": sym.get("ticker"),
            "name": s.get("name"),
            "series_id": s.get("seriesId"),
            "daily_value": s.get("dailyValue"),
            "market_cap": s.get("marketCap"),
        })
    return {"tab": tab, "securities": out}


# ── IPO: recent & upcoming IPOs ───────────────────────────────────────────────


def ipo(limit: int = 30) -> dict:
    """Upcoming and recent IPOs (ticker, date, status, price range)."""
    rows = api_client.get("/api/ipos") or []
    return {"ipos": rows[:limit]}


# ── FA: financials summary (delegates to analytics.financials) ────────────────


def fa(ticker: str, statement: str = "income_statement",
       period: str = "QTR", quarters: int = 8) -> dict:
    """Recent statement lines for a ticker via the new financials endpoint."""
    from analytics import financials as F
    df = F.fetch_financials(ticker, statement, period)
    tail = df.tail(quarters)
    periods = [d.strftime("%Y-%m-%d") for d in tail.index]
    lines = {col: [None if v != v else round(float(v), 4) for v in tail[col]]
             for col in tail.columns}
    return {"ticker": ticker.upper(), "statement": statement, "period": period,
            "periods": periods, "lines": lines}


# ── not-yet-discovered commands ───────────────────────────────────────────────

_CAPTURE_HINT = (
    "endpoint not captured yet — run `python -m server.token_server`, open this "
    "command's window in the terminal, then check "
    "`python -m server.cli discover`")


def tas(ticker: str) -> NotDiscovered:
    return NotDiscovered("TAS", _CAPTURE_HINT)


def halt() -> NotDiscovered:
    return NotDiscovered("HALT", _CAPTURE_HINT)


def tran(ticker: str) -> NotDiscovered:
    return NotDiscovered("TRAN", _CAPTURE_HINT)
