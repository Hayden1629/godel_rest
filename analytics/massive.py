"""Massive API client (Polygon-compatible) for fundamentals + reference data.

Confirmed endpoints (free key, 2026-06):
    GET /v2/aggs/ticker/{t}/range/1/day/{from}/{to}   daily bars   (see prices.py)
    GET /v3/reference/tickers/{t}                      company details / shares
    GET /vX/reference/financials?ticker=               income/balance/cash-flow

Financial line items arrive as {"value": x, "unit": ..., "label": ...}; helpers
flatten them to {key: value} for easy use.
"""
import time

import requests

from .config import massive_api_key

BASE = "https://api.massive.com"


def _get(path: str, params: dict | None = None, timeout: int = 25) -> dict:
    params = dict(params or {})
    params["apiKey"] = massive_api_key()
    start = time.perf_counter()
    status: int | str = "ERR"
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
        status = r.status_code
        if r.status_code == 429:
            raise RuntimeError("massive 429 rate limited")
        r.raise_for_status()
        return r.json()
    finally:
        try:  # logging must never break a data call
            from server.api_client import record
            record("GET", path, params, status,
                   (time.perf_counter() - start) * 1000, host="massive")
        except Exception:
            pass


def ticker_details(ticker: str) -> dict:
    """Company profile: name, market_cap, shares, sector, description."""
    res = _get(f"/v3/reference/tickers/{ticker}").get("results") or {}
    shares = (res.get("share_class_shares_outstanding")
              or res.get("weighted_shares_outstanding"))
    return {
        "ticker": res.get("ticker"),
        "name": res.get("name"),
        "market_cap": res.get("market_cap"),
        "shares_outstanding": shares,
        "sector": res.get("sic_description"),
        "employees": res.get("total_employees"),
        "exchange": res.get("primary_exchange"),
        "description": res.get("description"),
        "homepage": res.get("homepage_url"),
        "list_date": res.get("list_date"),
    }


def _flatten(stmt: dict) -> dict:
    out = {}
    for k, v in (stmt or {}).items():
        out[k] = v.get("value") if isinstance(v, dict) else v
    return out


def financials(ticker: str, timeframe: str = "annual", limit: int = 5) -> list[dict]:
    """Return statements newest-first. Each item flattens the three core
    statements plus period metadata."""
    data = _get("/vX/reference/financials",
                {"ticker": ticker, "timeframe": timeframe, "limit": limit,
                 "order": "desc", "sort": "period_of_report_date"})
    out = []
    for r in data.get("results") or []:
        f = r.get("financials") or {}
        out.append({
            "fiscal_period": r.get("fiscal_period"),
            "fiscal_year": r.get("fiscal_year"),
            "start_date": r.get("start_date"),
            "end_date": r.get("end_date"),
            "filing_date": r.get("filing_date"),
            "income": _flatten(f.get("income_statement")),
            "balance": _flatten(f.get("balance_sheet")),
            "cash_flow": _flatten(f.get("cash_flow_statement")),
        })
    return out
