"""Historical prices (HP) against api.godelterminal.com.

Reverse-engineered from examples/HP_call.har (the HISTORICAL_PRICES window's
open beacon → seriesId) + the actual data fetch seen in extendchat.har:

    POST /api/tv-advanced/bars
    {"seriesId":4678535,"resolution":"1D","from":...,"to":...,
     "countBack":300,"firstDataRequest":true,"hideAnomalies":true}
    -> {"data":{"data":[{open,high,low,close,volume,time(ms)},...],
                "metadata":{...}}, "error":null}

Resolves a ticker to its Gödel price series (server.ratio.resolve_series), pulls
OHLCV bars at a given resolution over a date window, and returns clean rows.
The `bars` endpoint honours `to` and `countBack` but ignores `from`, so the
window's lower bound is applied client-side.
"""
from __future__ import annotations

import math
import time

from . import api_client, ratio

BARS_PATH = "/api/tv-advanced/bars"

DEFAULT_RESOLUTION = "1D"
DEFAULT_MONTHS = 6
DAYS_PER_MONTH = 30

# user-friendly aliases -> the resolution string the API expects
_RESOLUTION_ALIASES = {
    "1D": "1D", "D": "1D", "DAY": "1D", "DAILY": "1D",
    "1W": "1W", "W": "1W", "WEEK": "1W", "WEEKLY": "1W",
    "1M": "1M", "M": "1M", "MONTH": "1M", "MONTHLY": "1M",
    "1": "1", "5": "5", "15": "15", "30": "30", "60": "60", "1H": "60",
}

# approximate bars per calendar day, to size countBack from a date window
_BARS_PER_DAY = {"1M": 1 / 30, "1W": 1 / 7, "1D": 1.0,
                 "60": 7.0, "30": 14.0, "15": 27.0, "5": 80.0, "1": 400.0}


class BadResolution(Exception):
    pass


def normalize_resolution(resolution: str) -> str:
    key = str(resolution).strip().upper()
    if key not in _RESOLUTION_ALIASES:
        raise BadResolution(
            f"unknown resolution {resolution!r}; try one of: "
            "1, 5, 15, 30, 60, 1D, 1W, 1M")
    return _RESOLUTION_ALIASES[key]


def _window(months: float, from_ts: int | None, to_ts: int | None) -> tuple[int, int]:
    to = int(to_ts) if to_ts is not None else int(time.time())
    if from_ts is not None:
        return int(from_ts), to
    return to - int(months * DAYS_PER_MONTH * 86400), to


def _count_back(resolution: str, frm: int, to: int) -> int:
    """Generously size countBack to cover [frm, to] at this resolution, since
    the API returns exactly `countBack` bars ending at `to` (ignoring `from`)."""
    days = max((to - frm) / 86400, 1)
    est = days * _BARS_PER_DAY.get(resolution, 1.0)
    return min(int(math.ceil(est * 1.15)) + 5, 5000)


def historical_prices(
    ticker: str,
    resolution: str = DEFAULT_RESOLUTION,
    months: float = DEFAULT_MONTHS,
    from_ts: int | None = None,
    to_ts: int | None = None,
    count_back: int | None = None,
) -> dict:
    """Fetch OHLCV bars for `ticker`.

    If `count_back` is given, returns exactly that many most-recent bars ending
    at `to` (the window lower bound is not applied). Otherwise the window
    [from, to] — explicit or `months` back from now — is honoured client-side.

    Returns:
      {ticker, name, series_id, resolution, window:{from,to},
       count, bars:[{date, time, open, high, low, close, volume}], raw}
    """
    res = normalize_resolution(resolution)
    s = ratio.resolve_series(ticker)
    frm, to = _window(months, from_ts, to_ts)

    explicit_count = count_back is not None
    cb = count_back if explicit_count else _count_back(res, frm, to)

    resp = api_client.post(BARS_PATH, {
        "seriesId": s.series_id,
        "resolution": res,
        "from": frm,
        "to": to,
        "countBack": int(cb),
        "firstDataRequest": True,
        "hideAnomalies": True,
    })
    if resp.get("error"):
        raise RuntimeError(f"bars error: {resp['error']}")

    raw_bars = ((resp.get("data") or {}).get("data")) or []
    bars = []
    for b in raw_bars:
        t_ms = b.get("time")
        if t_ms is None:
            continue
        t_s = t_ms // 1000
        if not explicit_count and t_s < frm:   # apply the ignored lower bound
            continue
        bars.append({
            "date": time.strftime("%Y-%m-%d", time.gmtime(t_s)),
            "time": t_s,
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
            "volume": b.get("volume"),
        })

    return {
        "ticker": s.ticker,
        "name": s.name,
        "series_id": s.series_id,
        "resolution": res,
        "window": {"from": frm, "to": to},
        "count": len(bars),
        "bars": bars,
        "raw": resp,
    }


def to_excel(result: dict, path: str) -> str:
    """Write bars to an .xlsx file. Requires pandas + openpyxl (analytics deps)."""
    import pandas as pd
    df = pd.DataFrame(result["bars"], columns=[
        "date", "time", "open", "high", "low", "close", "volume"])
    df.to_excel(path, index=False, sheet_name=f"{result['ticker']}_{result['resolution']}")
    return path


def to_csv(result: dict, path: str) -> str:
    import csv
    cols = ["date", "time", "open", "high", "low", "close", "volume"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(result["bars"])
    return path
