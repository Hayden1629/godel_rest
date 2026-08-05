"""Parse Gödel Terminal FA financial-statement exports (xlsx), compute trend
metrics, forecast them, and chart them over time.

The export is transposed: line items are rows, fiscal periods are columns
(e.g. "Q2 2008" ... "Q1 2026"). We load it into a tidy period-indexed frame so
revenue, margins, etc. are time series you can trend and forecast.
"""
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

QUARTER_END = {1: ("03", "31"), 2: ("06", "30"), 3: ("09", "30"), 4: ("12", "31")}
# matches "Q2 2008" (xlsx) and "Q2-2008" (API dataIndex)
_PERIOD_RE = re.compile(r"Q([1-4])[\s-]+(\d{4})", re.I)
STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")
_STATEMENT_ALIASES = {"cash_flow_statement": "cash_flow", "cashflow": "cash_flow"}

# ── new financials endpoint (Aug 2026) ────────────────────────────────────────
# The old /api/v1/consolidated_financials/{statement}/{series_id}/{period} route
# was retired (now 404s). The replacement returns *all three statements at once*
# as flat records keyed by an integer `seriesTypeId`, plus a self-describing
# `financialMetricTypes` id->name catalog:
#     GET /api/v1/terminal/financial-metric-group/legal-entities/{legalEntityId}
#         ?instrumentId={instrumentId}
# The legalEntityId + instrumentId are resolved from the seriesId via
# /api/v2/company-profile/{seriesId}. See resolve_entity().
_METRIC_GROUP_PATH = (
    "/api/v1/terminal/financial-metric-group/legal-entities/{leid}")

# seriesTypeId -> canonical slug. Names verified live from the endpoint's own
# financialMetricTypes catalog; slugs chosen to match _finalize()/model.py.
SERIES_TYPE_SLUG: dict[int, str] = {
    # income statement
    8: "revenue", 9: "cogs", 10: "gross_profit", 11: "sga", 12: "rnd",
    13: "opex", 14: "operating_income", 15: "other_income",
    16: "total_other_income", 19: "pretax_income", 20: "income_taxes",
    21: "net_income", 22: "net_income_cont", 23: "shares_basic",
    24: "shares_diluted", 26: "eps_diluted", 76: "ebit", 77: "ebitda",
    211: "total_revenue",
    # balance sheet
    27: "cash_equivalents", 30: "inventory", 31: "other_st_assets",
    32: "total_current_assets", 33: "ppe", 34: "lt_investments",
    35: "other_lt_assets", 36: "total_lt_assets", 37: "total_assets",
    38: "short_term_debt", 39: "accounts_payable", 41: "other_st_liabilities",
    42: "total_current_liabilities", 43: "long_term_debt",
    44: "other_lt_liabilities", 45: "total_liabilities", 46: "commitments",
    47: "common_stock", 48: "retained_earnings", 49: "aoci",
    50: "total_common_equity", 51: "equity_and_nci",
    52: "total_pref_common_equity", 53: "shareholder_equity",
    54: "total_liabilities_and_equity",
    # cash flow statement
    55: "depreciation", 56: "noncash_adjustments", 57: "operating_cash_flow",
    58: "operating_cash_flow_cont", 59: "cash_interest_paid",
    60: "cash_taxes_paid", 61: "capex", 62: "acquisitions",
    63: "purchase_investments", 64: "sale_investments", 65: "other_investing",
    66: "investing_cash_flow", 67: "investing_cash_flow_cont",
    68: "debt_repaid", 69: "buybacks", 70: "dividends_paid", 71: "debt_issued",
    72: "other_financing", 73: "financing_cash_flow",
    74: "financing_cash_flow_cont", 75: "net_change_cash",
}

# which seriesTypeIds belong to each statement (for per-statement splitting)
STATEMENT_TYPE_IDS: dict[str, frozenset[int]] = {
    "income_statement": frozenset(
        {8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23, 24, 26,
         76, 77, 211}),
    "balance_sheet": frozenset(
        {27, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45,
         46, 47, 48, 49, 50, 51, 52, 53, 54}),
    "cash_flow": frozenset(
        {55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70,
         71, 72, 73, 74, 75}),
}

_PERIODICITY = {"QTR": "QUARTERLY", "ANN": "ANNUAL", "SEMI": "SEMIANNUAL"}

# The new endpoint reports raw dollars / raw share counts; the rest of the code
# base (model.py valuation, charts, the retired xlsx export) works in millions.
# Scale every monetary line item and share count to millions on ingest; leave
# per-share figures (EPS) alone. Margins/pcts are derived later from ratios and
# are unaffected by a uniform scale.
_MILLIONS = 1e6
_UNSCALED_SLUGS = frozenset({"eps_diluted"})

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


def entity_from_series(series_id: int) -> dict:
    """Resolve the legalEntityId + instrumentId the new financials endpoint needs
    from a seriesId, via /api/v2/company-profile. Returns a dict with keys
    series_id, legal_entity_id, instrument_id, ticker, name."""
    from server import api_client
    prof = api_client.get(f"/api/v2/company-profile/{series_id}")
    counts = prof.get("marketCapSeriesIdsAndShareCounts") or []
    # prefer the entry that matches our seriesId; fall back to the first one
    entry = next((c for c in counts if c.get("seriesId") == series_id),
                 counts[0] if counts else None)
    if not entry:
        raise ValueError(f"no legal entity for series {series_id}")
    ctx = entry.get("seriesContext") or {}
    le = ctx.get("legalEntity") or {}
    inst = ctx.get("instrument") or {}
    if le.get("id") is None or inst.get("id") is None:
        raise ValueError(f"company profile for {series_id} lacks entity ids")
    return {
        "series_id": series_id,
        "legal_entity_id": le["id"],
        "instrument_id": inst["id"],
        "ticker": prof.get("ticker"),
        "name": prof.get("name"),
    }


def resolve_entity(ticker: str) -> dict:
    """Ticker -> {series_id, legal_entity_id, instrument_id, ticker, name}."""
    return entity_from_series(resolve_series_id(ticker))


@lru_cache(maxsize=64)
def fetch_metric_group(series_id: int) -> dict:
    """Raw payload from the financial-metric-group endpoint (all statements +
    the financialMetricTypes catalog), cached per series id so callers that
    want each statement separately don't re-hit a ~1.5MB endpoint three times."""
    from server import api_client
    ent = entity_from_series(series_id)
    return api_client.get(
        _METRIC_GROUP_PATH.format(leid=ent["legal_entity_id"]),
        params={"instrumentId": ent["instrument_id"]})


def _records_to_frame(records: list[dict], periodicity: str) -> pd.DataFrame:
    """Turn flat financialMetrics records into a period-indexed wide frame.

    Keeps only rows with a real `actual` for the requested periodicity, mapping
    seriesTypeId -> slug. Line items for one fiscal quarter can report on
    slightly different `periodEnd` dates (a few days' drift), so metrics are
    aligned on (fiscalYear, fiscalPeriod) and dated by that group's latest
    periodEnd. On restatements the newest `asOf` wins."""
    # fiscal_key -> {slug: (asOf, value)}, plus fiscal_key -> representative date
    cells: dict[tuple, dict[str, tuple[str, float]]] = {}
    dates: dict[tuple, str] = {}
    for r in records:
        if r.get("periodicity") != periodicity:
            continue
        slug = SERIES_TYPE_SLUG.get(r.get("seriesTypeId"))
        actual = r.get("actual")
        pe = r.get("periodEnd")
        if slug is None or actual is None or not pe:
            continue
        fkey = (r.get("fiscalYear"), r.get("fiscalPeriod"))
        as_of = r.get("asOf") or ""
        value = float(actual)
        if slug not in _UNSCALED_SLUGS:
            value /= _MILLIONS  # dollars/share-counts -> millions
        row = cells.setdefault(fkey, {})
        if slug not in row or as_of >= row[slug][0]:
            row[slug] = (as_of, value)
        if pe > dates.get(fkey, ""):
            dates[fkey] = pe

    if not cells:
        return pd.DataFrame()
    data: dict[str, dict] = {}
    for fkey, row in cells.items():
        idx = pd.Timestamp(dates[fkey])
        for slug, (_as_of, val) in row.items():
            data.setdefault(slug, {})[idx] = val
    df = pd.DataFrame(data)
    df.index.name = "period"
    return df[~df.index.duplicated()].sort_index()


def frame_from_payload(payload: dict, statement: str = "income_statement",
                       period: str = "QTR") -> pd.DataFrame:
    """Finalized period-indexed frame for one statement, built from a metric-group
    payload the caller already fetched (lets model.py reuse a single download)."""
    statement = _STATEMENT_ALIASES.get(statement, statement)
    periodicity = _PERIODICITY.get(period, period)
    type_ids = STATEMENT_TYPE_IDS.get(statement, frozenset())
    records = [r for r in payload.get("financialMetrics", [])
               if r.get("seriesTypeId") in type_ids]
    return _finalize(_records_to_frame(records, periodicity))


def fetch_statement_raw(series_id: int, statement: str = "income_statement",
                        period: str = "QTR") -> dict:
    """The metric-group slice for one statement — used for verbatim file export.
    Returns the same shape as the live endpoint (financialMetrics +
    financialMetricTypes + currency), filtered to this statement & period."""
    statement = _STATEMENT_ALIASES.get(statement, statement)
    periodicity = _PERIODICITY.get(period, period)
    payload = fetch_metric_group(series_id)
    type_ids = STATEMENT_TYPE_IDS.get(statement, frozenset())
    metrics = [r for r in payload.get("financialMetrics", [])
               if r.get("seriesTypeId") in type_ids
               and r.get("periodicity") == periodicity]
    types = [t for t in payload.get("financialMetricTypes", [])
             if t.get("id") in type_ids]
    return {
        "statement": statement,
        "period": period,
        "currency": payload.get("currency"),
        "financialMetricTypes": types,
        "financialMetrics": metrics,
    }


def fetch_statement(series_id: int, statement: str = "income_statement",
                    period: str = "QTR") -> pd.DataFrame:
    """Fetch one financial statement (QTR or ANN) for a series id."""
    return frame_from_payload(fetch_metric_group(series_id), statement, period)


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

