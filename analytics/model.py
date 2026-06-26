"""Deterministic single-ticker financial model.

Pulls all three quarterly statements (income, balance sheet, cash flow) straight
from Gödel Terminal, merges them into one period-indexed frame, derives the
standard modeling metrics an analyst looks at (revenue growth, margins, real free
cash flow), charts them over time, and runs an EV-based **reverse DCF** that
solves for the free-cash-flow growth rate the market is currently pricing in.

Free cash flow is real here: FCF = operating cash flow - capex (not the OCF
proxy used elsewhere). The reverse DCF targets *enterprise value*
(market cap + debt - cash), the correct claim against firm free cash flow.

Inputs and where they come from (all verified working, no DOM scraping):
  - income / balance / cash-flow statements .... Gödel /consolidated_financials
  - share price (last close) .................... Gödel /api/v1/search
  - shares outstanding + market cap ............. Massive /v3/reference/tickers
  - net debt (total debt - cash) ................ Gödel balance sheet

CLI: python3 -m analytics.run model TICKER [--period QTR|ANN] [--forecast 8]
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

from . import financials as F
from . import massive

OUTPUT_DIR = Path("output")

# the chart panel an analyst typically wants to eyeball (income + cash flow)
MODEL_ITEMS = [
    "revenue", "revenue_yoy", "gross_margin", "operating_margin",
    "net_margin", "net_income", "fcf", "fcf_margin",
]
# balance-sheet trends — shown as a fallback when a name only has a balance sheet
BALANCE_ITEMS = ["cash_equivalents", "total_debt", "net_debt",
                 "total_common_equity", "total_assets"]


def chartable(df: pd.DataFrame, candidates: list[str]) -> list[str]:
    """Candidates that are present AND have at least one real value to plot."""
    return [c for c in candidates
            if c in df.columns and df[c].notna().any()]


def has_income_data(df: pd.DataFrame) -> bool:
    return "revenue" in df.columns and df["revenue"].notna().any()


# ── data assembly ─────────────────────────────────────────────────────────────

def download_statements(ticker: str, period: str = "QTR",
                        outdir: Path = OUTPUT_DIR) -> dict:
    """Fetch the three statements and persist each as CSV (tidy, period-indexed)
    and the verbatim Gödel JSON. Returns {statement: DataFrame}."""
    series_id = F.resolve_series_id(ticker)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    frames: dict[str, pd.DataFrame] = {}
    for statement in F.STATEMENTS:
        # one GET: keep the raw payload for the file export and build the frame
        # from the same payload (no second round-trip).
        raw = F.fetch_statement_raw(series_id, statement, period)
        df = F.frame_from_payload(raw)
        stem = f"{ticker.upper()}_{statement}_{period}"
        df.to_csv(outdir / f"{stem}.csv")
        (outdir / f"{stem}.json").write_text(json.dumps(raw, indent=1))
        frames[statement] = df
    return frames


def build_model_frame(frames: dict) -> pd.DataFrame:
    """Outer-join the statements on the period index and derive cross-statement
    metrics (FCF margin needs revenue from income + FCF from cash flow)."""
    income = frames.get("income_statement", pd.DataFrame())
    balance = frames.get("balance_sheet", pd.DataFrame())
    cash = frames.get("cash_flow", pd.DataFrame())

    # prefix-free join: keep first occurrence of duplicate columns (net_income
    # appears on both income & cash flow; income's is authoritative)
    merged = income.copy()
    for other in (cash, balance):
        new_cols = [c for c in other.columns if c not in merged.columns]
        merged = merged.join(other[new_cols], how="outer")
    merged = merged.sort_index()

    if {"fcf", "revenue"} <= set(merged.columns):
        merged["fcf_margin"] = merged["fcf"] / merged["revenue"]
    if {"short_term_debt", "long_term_debt"} <= set(merged.columns):
        merged["total_debt"] = (merged["short_term_debt"].fillna(0)
                                + merged["long_term_debt"].fillna(0))
    if {"total_debt", "cash_equivalents"} <= set(merged.columns):
        merged["net_debt"] = merged["total_debt"] - merged["cash_equivalents"].fillna(0)
    return merged


# ── valuation inputs ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Valuation:
    ticker: str
    price: float | None          # Gödel last close
    shares_outstanding: float | None
    market_cap: float | None     # USD
    total_debt: float | None     # millions (statement units)
    cash: float | None           # millions
    net_debt: float | None       # millions
    enterprise_value: float | None  # USD
    fcf_ttm: float | None        # millions (last 4 reported quarters)
    source_price: str = ""


def _last_close(ticker: str) -> float | None:
    """Gödel search returns last_close for the matched instrument."""
    from server import api_client
    res = api_client.get("/api/v1/search", params={"query": ticker})
    for inst in res.get("instruments", []):
        if (inst.get("ticker") or "").upper() == ticker.upper():
            return inst.get("last_close")
    insts = res.get("instruments", [])
    return insts[0].get("last_close") if insts else None


def build_valuation(ticker: str, model_df: pd.DataFrame) -> Valuation:
    """Assemble enterprise value. Statement values are in millions; market cap
    is in dollars — net debt is converted to dollars (×1e6) for the EV bridge."""
    price = _last_close(ticker)
    details = {}
    try:
        details = massive.ticker_details(ticker)
    except Exception:
        details = {}
    shares = details.get("shares_outstanding")
    mcap = details.get("market_cap")
    source = "godel_last_close"
    # prefer price*shares for market cap so EV is internally consistent with the
    # quoted price; fall back to Massive's market cap if shares are missing.
    if price and shares:
        mcap = price * shares
        source = "price*shares"
    elif not mcap and price and shares:
        mcap = price * shares

    def _latest(col):
        if col in model_df.columns:
            s = model_df[col].dropna()
            return float(s.iloc[-1]) if len(s) else None
        return None

    total_debt = _latest("total_debt")
    cash = _latest("cash_equivalents")
    net_debt = _latest("net_debt")
    fcf_ttm = _latest("fcf_ttm")

    ev = None
    if mcap is not None and net_debt is not None:
        ev = mcap + net_debt * 1e6  # net_debt millions -> dollars
    return Valuation(
        ticker=ticker.upper(), price=price, shares_outstanding=shares,
        market_cap=mcap, total_debt=total_debt, cash=cash, net_debt=net_debt,
        enterprise_value=ev, fcf_ttm=fcf_ttm, source_price=source,
    )


# ── company profile / overview (the DES-style fields) ─────────────────────────

def _ytd_return(ticker: str) -> float | None:
    """Year-to-date price return from the Massive daily bars."""
    from . import research
    from datetime import date
    bars = research._bars(ticker, lookback_days=400)
    if not bars:
        return None
    year = date.today().year
    closes = [b["c"] for b in bars if b.get("c")]
    # first bar on/after Jan 1 of the current year
    base = next((b["c"] for b in bars
                 if b.get("c") and b["t"] / 1000 >= date(year, 1, 1).toordinal()
                 and date.fromtimestamp(b["t"] / 1000).year == year), None)
    if base is None or not closes:
        return None
    return closes[-1] / base - 1


def company_card(ticker: str, lookback_days: int = 365) -> dict:
    """DES-style overview assembled from Massive (profile) + price history.

    NOTE: this is NOT the Gödel 'DES' command (that endpoint is a dead DOM
    scraper). Name/summary/sector come from Massive `ticker_details`; beta and
    returns come from the Massive daily price history vs SPY.
    """
    from . import research, thesis
    details, summary, risk = {}, {}, {}
    try:
        details = massive.ticker_details(ticker)
    except Exception:
        pass
    try:
        summary = research.price_summary(ticker, lookback_days)
    except Exception:
        pass
    try:
        risk = thesis.price_risk(ticker, lookback_days)
    except Exception:
        pass
    return {
        "name": details.get("name"),
        "description": details.get("description"),
        "sector": details.get("sector"),
        "exchange": details.get("exchange"),
        "employees": details.get("employees"),
        "homepage": details.get("homepage"),
        "last": summary.get("last") or risk.get("last"),
        "ytd": _ytd_return(ticker),
        "ret_1y": summary.get("ret_1y"),
        "high_52w": summary.get("high_52w"), "low_52w": summary.get("low_52w"),
        "beta": risk.get("beta"), "annual_vol": risk.get("annual_vol"),
        "sharpe": risk.get("sharpe"), "max_drawdown": risk.get("max_drawdown"),
        "low_confidence_beta": risk.get("low_confidence_beta"),
    }


def price_chart(ticker: str, outdir: Path = OUTPUT_DIR,
                lookback_days: int = 400) -> str | None:
    from . import research
    try:
        return research.save_price_chart(
            ticker, out_path=str(Path(outdir) / f"{ticker}_price.png"),
            lookback_days=lookback_days)
    except Exception:
        return None


# ── reverse DCF on enterprise value ───────────────────────────────────────────

def _dcf_ev(fcf: float, g: float, r: float, tg: float, years: int) -> float:
    """PV of `years` of FCF growing at g, plus Gordon terminal, discounted at r."""
    pv, cf = 0.0, fcf
    for t in range(1, years + 1):
        cf = fcf * (1 + g) ** t
        pv += cf / (1 + r) ** t
    terminal = cf * (1 + tg) / (r - tg)
    return pv + terminal / (1 + r) ** years


def _hist_fcf_cagr(model_df: pd.DataFrame) -> float | None:
    """CAGR of TTM free cash flow over the available history (annualized)."""
    if "fcf_ttm" not in model_df.columns:
        return None
    s = model_df["fcf_ttm"].dropna()
    s = s[s > 0]
    if len(s) < 5:
        return None
    years = (s.index[-1] - s.index[0]).days / 365.25
    if years <= 0 or s.iloc[0] <= 0:
        return None
    return (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1


def reverse_dcf(val: Valuation, model_df: pd.DataFrame,
                discount_rate: float = 0.10, terminal_growth: float = 0.025,
                years: int = 10) -> dict:
    """Solve the constant FCF growth rate that makes the discounted FCF stream
    equal today's enterprise value. FCF base = TTM FCF in dollars."""
    ev = val.enterprise_value
    fcf = (val.fcf_ttm * 1e6) if val.fcf_ttm is not None else None
    if not ev or not fcf or fcf <= 0:
        return {"ticker": val.ticker,
                "error": "need positive TTM FCF and an enterprise value",
                "enterprise_value": ev, "fcf_ttm": val.fcf_ttm}

    lo, hi = -0.50, 1.00
    for _ in range(200):
        mid = (lo + hi) / 2
        if _dcf_ev(fcf, mid, discount_rate, terminal_growth, years) > ev:
            hi = mid
        else:
            lo = mid
    implied = (lo + hi) / 2
    hist = _hist_fcf_cagr(model_df)
    return {
        "ticker": val.ticker,
        "enterprise_value": round(ev, 0),
        "fcf_ttm_usd": round(fcf, 0),
        "ev_to_fcf": round(ev / fcf, 1),
        "assumptions": {"discount_rate": discount_rate,
                        "terminal_growth": terminal_growth, "years": years},
        "implied_fcf_growth": round(implied, 4),
        "historical_fcf_cagr": round(hist, 4) if hist is not None else None,
        "read": _read(implied, hist),
    }


def _read(implied: float, hist: float | None) -> str:
    if hist is None:
        return (f"Market prices in ~{implied:.1%} annual FCF growth (no clean "
                "positive FCF history to compare against — treat with caution).")
    if implied > hist + 0.02:
        return (f"Market prices in {implied:.1%} FCF growth vs {hist:.1%} "
                "historical — priced for acceleration (demanding).")
    if implied < hist - 0.02:
        return (f"Market prices in {implied:.1%} FCF growth vs {hist:.1%} "
                "historical — low bar (cheap if the trend holds).")
    return (f"Market prices in {implied:.1%} FCF growth, ~in line with {hist:.1%} "
            "historical.")


# ── orchestration ─────────────────────────────────────────────────────────────

def run(ticker: str, period: str = "QTR", forecast: int = 8,
        discount_rate: float = 0.10, terminal_growth: float = 0.025,
        years: int = 10, outdir: Path = OUTPUT_DIR) -> dict:
    """Full pipeline: download -> model -> chart -> EV -> reverse DCF."""
    ticker = ticker.upper()
    frames = download_statements(ticker, period, outdir)
    model_df = build_model_frame(frames)

    chart = Path(outdir) / f"{ticker}_model.png"
    items = chartable(model_df, MODEL_ITEMS) or chartable(model_df, BALANCE_ITEMS)
    if items:
        F.plot_trends(model_df, items=items, forecast_periods=forecast,
                      ticker=ticker, out_path=str(chart))
    else:
        chart = None

    val = build_valuation(ticker, model_df)
    rdcf = reverse_dcf(val, model_df, discount_rate, terminal_growth, years)

    summary = {"ticker": ticker, "valuation": asdict(val), "reverse_dcf": rdcf,
               "chart": str(chart) if chart else None,
               "files": sorted(str(p) for p in Path(outdir).glob(f"{ticker}_*"))}
    (Path(outdir) / f"{ticker}_model_summary.json").write_text(
        json.dumps(summary, indent=2, default=str))
    return summary
