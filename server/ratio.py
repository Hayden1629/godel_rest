"""Ratio / regression analysis against api.godelterminal.com.

Resolves a ticker symbol to its Gödel `primary_series_id` via /api/v1/search,
then POSTs /api/v1/ratio-analysis to regress one series (y) against another (x)
over a time window. Returns the full regression block (beta, alpha, r, rsquared,
std errors, t/p values, ...) plus correlation and ratio summaries — so all the
values you'd otherwise read off the terminal come back in one call.

Captured from examples/ratio.har (AMD ÷ SOX, 6-month window):
    POST /api/v1/ratio-analysis
    {"ySeriesId":4678082,"xSeriesId":4141197,"from":...,"to":...,
     "regressionIncluded":true,"correlationIncluded":true,"correlationWindow":120}
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import api_client

SEARCH_PATH = "/api/v1/search"
RATIO_PATH = "/api/v1/ratio-analysis"

# default window the terminal uses for the rolling correlation series
DEFAULT_CORRELATION_WINDOW = 120
DEFAULT_MONTHS = 6
DAYS_PER_MONTH = 30  # calendar-month approximation for the lookback window


class TickerNotFound(Exception):
    pass


@dataclass(frozen=True)
class Series:
    """A resolved instrument: ticker symbol -> price series id."""
    ticker: str
    series_id: int
    name: str


def resolve_series(ticker: str) -> Series:
    """Resolve a ticker symbol to its primary price-series id via search.

    Picks the first instrument whose ticker matches exactly (case-insensitive);
    falls back to the first instrument that carries a primary_series_id."""
    sym = ticker.strip().upper()
    if not sym:
        raise TickerNotFound("empty ticker")
    resp = api_client.get(SEARCH_PATH, params={
        "query": sym,
        "types": "instruments",
        "hasExtantRelationshipWithAnyOf": "AGGREGATE_RTH",
    })
    instruments = resp.get("instruments") or []
    exact = [i for i in instruments
             if (i.get("ticker") or "").upper() == sym and i.get("primary_series_id")]
    pick = exact[0] if exact else next(
        (i for i in instruments if i.get("primary_series_id")), None)
    if not pick:
        raise TickerNotFound(
            f"no instrument with a price series found for {ticker!r}")
    return Series(
        ticker=pick.get("ticker") or sym,
        series_id=int(pick["primary_series_id"]),
        name=pick.get("name") or "",
    )


def _window(months: float, from_ts: int | None, to_ts: int | None) -> tuple[int, int]:
    """Resolve an explicit [from, to] unix-second window, or derive one that
    ends now and spans `months` back (default 6)."""
    to = int(to_ts) if to_ts is not None else int(time.time())
    if from_ts is not None:
        return int(from_ts), to
    span = int(months * DAYS_PER_MONTH * 86400)
    return to - span, to


def ratio_analysis(
    y_ticker: str,
    x_ticker: str,
    months: float = DEFAULT_MONTHS,
    correlation_window: int = DEFAULT_CORRELATION_WINDOW,
    from_ts: int | None = None,
    to_ts: int | None = None,
) -> dict:
    """Regress `y_ticker` against `x_ticker` over the given window.

    Returns a flat, immutable-friendly summary dict:
      {y, x, window:{from,to,months}, regression:{...full block...},
       correlation:{last,low,high}, ratio:{last,low,high},
       ylast, xlast}
    plus `raw` holding the untouched API payload (timeseries included)."""
    y = resolve_series(y_ticker)
    x = resolve_series(x_ticker)
    frm, to = _window(months, from_ts, to_ts)

    body = {
        "ySeriesId": y.series_id,
        "xSeriesId": x.series_id,
        "from": frm,
        "to": to,
        "regressionIncluded": True,
        "correlationIncluded": True,
        "correlationWindow": int(correlation_window),
    }
    resp = api_client.post(RATIO_PATH, body)

    if resp.get("error"):
        raise RuntimeError(f"ratio-analysis error: {resp['error']}")

    reg = resp.get("regression") or {}
    corr = resp.get("correlation") or {}
    rat = resp.get("ratioData") or {}
    ydata = resp.get("ydata") or {}
    xdata = resp.get("xdata") or {}

    # drop the bulky rolling-regression `values` array from the summary view
    regression = {k: v for k, v in reg.items() if k != "values"}

    return {
        "y": {"ticker": y.ticker, "name": y.name, "series_id": y.series_id},
        "x": {"ticker": x.ticker, "name": x.name, "series_id": x.series_id},
        "window": {"from": frm, "to": to, "months": months},
        "regression": regression,
        "correlation": {
            "last": corr.get("lastValue"),
            "low": corr.get("low"),
            "high": corr.get("high"),
            "window": int(correlation_window),
        },
        "ratio": {
            "last": rat.get("lastValue"),
            "low": rat.get("low"),
            "high": rat.get("high"),
        },
        "ylast": ydata.get("lastValue"),
        "xlast": xdata.get("lastValue"),
        "raw": resp,
    }
