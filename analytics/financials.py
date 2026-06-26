"""Parse Gödel Terminal FA financial-statement exports (xlsx), compute trend
metrics, forecast them, and chart them over time.

The export is transposed: line items are rows, fiscal periods are columns
(e.g. "Q2 2008" ... "Q1 2026"). We load it into a tidy period-indexed frame so
revenue, margins, etc. are time series you can trend and forecast.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

QUARTER_END = {1: ("03", "31"), 2: ("06", "30"), 3: ("09", "30"), 4: ("12", "31")}
# matches "Q2 2008" (xlsx) and "Q2-2008" (API dataIndex)
_PERIOD_RE = re.compile(r"Q([1-4])[\s-]+(\d{4})", re.I)
# NB: the cash-flow endpoint key is "cash_flow" — "cash_flow_statement" 404s to
# an empty {columns:[],entries:[]} payload. Kept the alias in CLI for back-compat.
STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")
_STATEMENT_ALIASES = {"cash_flow_statement": "cash_flow", "cashflow": "cash_flow"}

# friendly aliases for Gödel income-statement row labels — covers both the xlsx
# export ("Revenue") and the raw API ("Total Revenue", "SG&A" -> sg_a slug).
ALIASES = {
    "revenue": "revenue", "total_revenue": "revenue",
    "operating_revenue": "operating_revenue",
    "cogs": "cogs", "total_cost_of_revenue": "cogs",
    "gross_profit": "gross_profit", "total_gross_profit": "gross_profit",
    "sga_expense": "sga", "sg_a_expense": "sga",
    "rd_expense": "rnd", "r_d_expense": "rnd",
    "operating_expenses": "opex", "total_operating_expenses": "opex",
    "operating_income": "operating_income",
    "total_operating_income": "operating_income",
    "other_income": "other_income", "total_other_income": "total_other_income",
    "pretax_income": "pretax_income", "total_pretax_income": "pretax_income",
    "income_taxes": "income_taxes", "net_income": "net_income",
    "gross_profit_margin": "gross_margin",
    "operating_profit_margin": "operating_margin",
    "net_profit_margin": "net_margin", "rd_as_of_revenue": "rnd_pct",
    "r_d_as_of_revenue": "rnd_pct", "sga_as_of_revenue": "sga_pct",
    "sg_a_as_of_revenue": "sga_pct", "revenue_qoq_growth": "revenue_qoq",
    # cash-flow statement line items
    "net_cash_from_operations": "operating_cash_flow",
    "net_cash_from_continuing_operating_activities": "operating_cash_flow_cont",
    "purchase_of_pp_e": "capex",
    "depreciation_expense": "depreciation",
    "payment_of_dividends": "dividends_paid",
    "repurchase_comn_stock": "buybacks",
    "issuance_of_debt": "debt_issued", "debt_repayment": "debt_repaid",
}


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower()).strip("_")
    return ALIASES.get(s, s)


def _period_to_date(label: str):
    m = _PERIOD_RE.search(str(label))
    if not m:
        return None
    q, year = int(m.group(1)), int(m.group(2))
    mm, dd = QUARTER_END[q]
    return pd.Timestamp(f"{year}-{mm}-{dd}")


def _to_number(v):
    if v is None:
        return np.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "N/A", "NA", "-", "—"):
        return np.nan
    pct = s.endswith("%")
    s = s.rstrip("%")
    try:
        x = float(s)
    except ValueError:
        return np.nan
    return x / 100.0 if pct else x


def parse_godel_fa(path: str | Path) -> pd.DataFrame:
    """Return a period-indexed DataFrame: index = quarter-end dates, columns =
    normalized line items / margins. Margins are fractions (0.70 = 70%)."""
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))

    # find the header row: the one with the most "Q# YYYY" cells
    hdr_idx = max(range(len(rows)),
                  key=lambda i: sum(bool(_PERIOD_RE.search(str(c)))
                                    for c in rows[i] if c))
    header = rows[hdr_idx]
    col_dates = [( j, _period_to_date(c)) for j, c in enumerate(header)
                 if c and _period_to_date(c) is not None]

    data = {}
    for r in rows[hdr_idx + 1:]:
        label = r[0]
        if not label:
            continue
        key = _slug(label)
        data[key] = [_to_number(r[j]) if j < len(r) else np.nan
                     for j, _ in col_dates]
    dates = [d for _, d in col_dates]
    df = pd.DataFrame(data, index=pd.DatetimeIndex(dates, name="period"))
    df = df[~df.index.duplicated()].sort_index()
    return _finalize(df)


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived margins / TTM / YoY columns if the source omitted them."""
    if "revenue" not in df and "operating_revenue" in df:
        df["revenue"] = df["operating_revenue"]
    if "net_margin" not in df and {"net_income", "revenue"} <= set(df):
        df["net_margin"] = df["net_income"] / df["revenue"]
    if "gross_margin" not in df and {"gross_profit", "revenue"} <= set(df):
        df["gross_margin"] = df["gross_profit"] / df["revenue"]
    if "operating_margin" not in df and {"operating_income", "revenue"} <= set(df):
        df["operating_margin"] = df["operating_income"] / df["revenue"]
    if "revenue" in df:
        df["revenue_ttm"] = df["revenue"].rolling(4).sum()
        df["revenue_yoy"] = df["revenue"].pct_change(4)
    if "net_income" in df:
        df["net_income_ttm"] = df["net_income"].rolling(4).sum()
    # free cash flow = operating cash flow - capex (capex is reported negative)
    if "fcf" not in df and {"operating_cash_flow", "capex"} <= set(df):
        df["fcf"] = df["operating_cash_flow"] + df["capex"]
        df["fcf_ttm"] = df["fcf"].rolling(4).sum()
    return df


def fetch_statement_raw(series_id: int, statement: str = "income_statement",
                        period: str = "QTR") -> dict:
    """Raw {columns, entries} payload — used for verbatim file export."""
    from server import api_client
    statement = _STATEMENT_ALIASES.get(statement, statement)
    return api_client.get(
        f"/api/v1/consolidated_financials/{statement}/{series_id}/{period}")


# ── fetch straight from the Gödel API (no manual xlsx download) ────────────────

def resolve_series_id(ticker: str) -> int:
    """Resolve a ticker to its Gödel primary series id via search."""
    from server import api_client
    res = api_client.get("/api/v1/search", params={"query": ticker})
    for inst in res.get("instruments", []):
        if (inst.get("ticker") or "").upper() == ticker.upper():
            return inst["primary_series_id"]
    insts = res.get("instruments", [])
    if insts:
        return insts[0]["primary_series_id"]
    raise ValueError(f"no instrument found for {ticker!r}")


def _frame_from_api(payload: dict) -> pd.DataFrame:
    """Build a period-indexed frame from the {columns, entries} API shape."""
    col_dates = []
    for c in payload.get("columns", []):
        d = _period_to_date(c.get("dataIndex") or c.get("title"))
        if d is not None:
            col_dates.append((c.get("dataIndex"), d))
    data, idx = {}, [d for _, d in col_dates]
    for e in payload.get("entries", []):
        label = e.get("item") or e.get("key")
        if not label:
            continue
        data[_slug(label)] = [_to_number(e.get(di)) for di, _ in col_dates]
    df = pd.DataFrame(data, index=pd.DatetimeIndex(idx, name="period"))
    df = df[~df.index.duplicated()].sort_index()
    return df


def frame_from_payload(payload: dict) -> pd.DataFrame:
    """Build the finalized period-indexed frame from a raw {columns,entries}
    payload — lets callers reuse a payload they already fetched."""
    return _finalize(_frame_from_api(payload))


def fetch_statement(series_id: int, statement: str = "income_statement",
                    period: str = "QTR") -> pd.DataFrame:
    """Fetch one financial statement (QTR or ANN) for a series id."""
    from server import api_client
    statement = _STATEMENT_ALIASES.get(statement, statement)
    payload = api_client.get(
        f"/api/v1/consolidated_financials/{statement}/{series_id}/{period}")
    return _finalize(_frame_from_api(payload))


def fetch_financials(ticker: str, statement: str = "income_statement",
                     period: str = "QTR") -> pd.DataFrame:
    """Resolve a ticker and fetch its financial statement — the programmatic
    equivalent of `<TICKER> EQ FA` + download."""
    return fetch_statement(resolve_series_id(ticker), statement, period)


# ── forecasting ───────────────────────────────────────────────────────────────

def _future_index(last, periods):
    return pd.date_range(start=last, periods=periods + 1, freq="QE")[1:]


def _linear_forecast(s: pd.Series, periods: int):
    x = np.arange(len(s))
    coef = np.polyfit(x, s.values, 1)
    fx = np.arange(len(s), len(s) + periods)
    fc = pd.Series(np.polyval(coef, fx), index=_future_index(s.index[-1], periods))
    resid = s.values - np.polyval(coef, x)
    return fc, float(np.std(resid))


def forecast(series: pd.Series, periods: int = 8, seasonal_periods: int = 4):
    """Forecast `periods` quarters ahead. Uses damped Holt-Winters (additive
    trend + seasonal when there's enough history), falling back to a linear
    trend. Returns (forecast_series, residual_std) for plotting bands."""
    import warnings
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    s = series.dropna()
    if len(s) < 6:
        return (pd.Series(dtype=float), 0.0) if len(s) < 2 else _linear_forecast(s, periods)
    try:
        use_seasonal = len(s) >= 2 * seasonal_periods
        # statsmodels wants a clean integer index when periods have gaps
        s_fit = s.reset_index(drop=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ExponentialSmoothing(
                s_fit, trend="add", damped_trend=True,
                seasonal="add" if use_seasonal else None,
                seasonal_periods=seasonal_periods if use_seasonal else None,
                initialization_method="estimated").fit()
            fc_vals = fit.forecast(periods)
        fc = pd.Series(np.asarray(fc_vals),
                       index=_future_index(s.index[-1], periods))
        return fc, float(np.nanstd(fit.resid))
    except Exception:
        return _linear_forecast(s, periods)


# ── charts ────────────────────────────────────────────────────────────────────

DEFAULT_ITEMS = ["revenue", "net_income", "gross_margin", "operating_margin",
                 "net_margin", "revenue_yoy"]
_IS_PCT = {"gross_margin", "operating_margin", "net_margin", "revenue_yoy",
           "revenue_qoq", "rnd_pct", "sga_pct", "fcf_margin"}

_PRETTY = {
    "revenue": "Revenue", "net_income": "Net Income",
    "gross_margin": "Gross Margin", "operating_margin": "Operating Margin",
    "net_margin": "Net Margin", "revenue_yoy": "Revenue YoY",
    "revenue_ttm": "Revenue (TTM)", "net_income_ttm": "Net Income (TTM)",
    "operating_income": "Operating Income",
    "fcf": "Free Cash Flow", "fcf_ttm": "Free Cash Flow (TTM)",
    "fcf_margin": "FCF Margin", "operating_cash_flow": "Operating Cash Flow",
    "capex": "Capex",
    "cash_equivalents": "Cash & Equivalents", "total_debt": "Total Debt",
    "net_debt": "Net Debt", "total_common_equity": "Common Equity",
    "total_assets": "Total Assets",
}


def plot_trends(df: pd.DataFrame, items=None, forecast_periods: int = 8,
                ticker: str = "", out_path: str | None = None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    items = [i for i in (items or DEFAULT_ITEMS) if i in df.columns]
    if not items:
        raise ValueError("none of the requested items are in the data")

    n = len(items)
    ncols = 2 if n > 1 else 1
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(13, 3.2 * nrows), squeeze=False)
    axes = axes.flatten()

    for ax, item in zip(axes, items):
        s = df[item].dropna()
        ax.plot(s.index, s.values, color="#1f77b4", lw=1.4, label="actual")
        if forecast_periods:
            fc, std = forecast(s, forecast_periods)
            if len(fc):
                ax.plot(fc.index, fc.values, color="#d62728", lw=1.4, ls="--",
                        label="forecast")
                ax.fill_between(fc.index, fc.values - std, fc.values + std,
                                color="#d62728", alpha=0.15)
        ax.set_title(_PRETTY.get(item, item), fontsize=11)
        ax.grid(alpha=0.3)
        if item in _IS_PCT:
            ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        ax.axhline(0, color="#888", lw=0.6)
    for ax in axes[len(items):]:
        ax.set_visible(False)
    axes[0].legend(loc="best", fontsize=8)

    fig.suptitle(f"{ticker or 'Financials'} — quarterly trends + {forecast_periods}q forecast",
                 fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    out_path = out_path or f"output/{(ticker or 'financials')}_trends.png"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def forecast_table(df: pd.DataFrame, items=None, periods: int = 8) -> pd.DataFrame:
    """A tidy forecast table (periods x items) for programmatic use."""
    items = [i for i in (items or DEFAULT_ITEMS) if i in df.columns]
    cols = {}
    for item in items:
        fc, _ = forecast(df[item].dropna(), periods)
        cols[item] = fc
    return pd.DataFrame(cols)

