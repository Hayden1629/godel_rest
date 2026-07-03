"""Beta-neutral pair-trade builder on top of the ratio-analysis tool.

Given two tickers (e.g. PLTR / HIMS) it answers: *is this a tradeable pair, which
way is it stretched right now, and how do I size the two legs so the position is
market-beta neutral?*

Procedure
---------
1. Pair regression A-on-B (server.ratio) → correlation + r² gauge whether the two
   move together enough to pair-trade, and hand back the A/B price-ratio series.
2. Mean-reversion signal: z-score of the current ratio vs its own history.
   ratio stretched high → A rich vs B → SHORT A / LONG B (bet the ratio falls);
   stretched low → the mirror. Inside the band → no trade.
3. Market beta of each leg (regress each vs a benchmark, default SPY).
4. Beta-neutral sizing: split capital so β_long·$long = β_short·$short, i.e. the
   two legs' market exposures cancel (net portfolio beta ≈ 0). Convert to shares
   at the last price.

Nothing here places orders — it only emits the plan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import ratio

DEFAULT_BENCHMARK = "SPY"
DEFAULT_CAPITAL = 10_000.0
DEFAULT_ENTRY_Z = 1.5      # |z| above this = enter; inside = stand aside
MIN_TRADEABLE_R = 0.5      # warn below this correlation — pair too loose


@dataclass(frozen=True)
class Leg:
    ticker: str
    side: str          # "LONG" or "SHORT"
    beta: float        # market beta (vs benchmark)
    price: float
    dollars: float
    shares: int


def _ratio_stats(timeseries: dict) -> dict:
    """Mean, std, current z-score, percentile and AR(1) half-life of the
    A/B price-ratio series. Pure numpy-free math so it works without pandas."""
    pts = [v for _, v in sorted(timeseries.items(), key=lambda kv: int(kv[0]))]
    n = len(pts)
    if n < 3:
        return {"n": n, "mean": None, "std": None, "z": None,
                "percentile": None, "half_life_days": None}
    mean = sum(pts) / n
    var = sum((p - mean) ** 2 for p in pts) / (n - 1)
    std = math.sqrt(var)
    last = pts[-1]
    z = (last - mean) / std if std else 0.0
    percentile = sum(1 for p in pts if p <= last) / n
    return {"n": n, "mean": mean, "std": std, "z": z,
            "percentile": percentile, "half_life_days": _half_life(pts)}


def _half_life(pts: list[float]) -> float | None:
    """Expected days for the ratio to close half the gap to its mean, from an
    AR(1) fit: Δr_t = a + b·r_{t-1}; half-life = -ln(2)/ln(1+b) for -2<b<0."""
    lag = pts[:-1]
    delta = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    m = len(lag)
    if m < 2:
        return None
    mx = sum(lag) / m
    my = sum(delta) / m
    sxx = sum((x - mx) ** 2 for x in lag)
    if sxx == 0:
        return None
    b = sum((lag[i] - mx) * (delta[i] - my) for i in range(m)) / sxx
    if b >= 0 or (1 + b) <= 0:
        return None  # not mean-reverting
    return -math.log(2) / math.log(1 + b)


def _signal(z: float, entry_z: float, a: str, b: str) -> tuple[str, str, str]:
    """Map the z-score to (action, long_ticker, short_ticker)."""
    if z >= entry_z:        # ratio rich → A expensive vs B
        return f"SHORT {a} / LONG {b}", b, a
    if z <= -entry_z:       # ratio cheap → A cheap vs B
        return f"LONG {a} / SHORT {b}", a, b
    return "NEUTRAL — ratio inside band, stand aside", "", ""


def _size_beta_neutral(long_t: str, short_t: str, betas: dict,
                       prices: dict, capital: float) -> tuple[Leg, Leg, float]:
    """Split `capital` (gross) so β_long·$long = β_short·$short → net beta ≈ 0.
    Uses |beta| so a negative-beta leg is still hedged sensibly."""
    bl, bs = abs(betas[long_t]) or 1e-9, abs(betas[short_t]) or 1e-9
    # $short = $long·(bl/bs);  $long + $short = capital
    dollars_long = capital / (1 + bl / bs)
    dollars_short = capital - dollars_long
    long_leg = Leg(long_t, "LONG", betas[long_t], prices[long_t],
                   round(dollars_long, 2),
                   int(dollars_long / prices[long_t]) if prices[long_t] else 0)
    short_leg = Leg(short_t, "SHORT", betas[short_t], prices[short_t],
                    round(dollars_short, 2),
                    int(dollars_short / prices[short_t]) if prices[short_t] else 0)
    # net market beta of the book: +long exposure, -short exposure
    net = (long_leg.beta * long_leg.dollars
           - short_leg.beta * short_leg.dollars)
    return long_leg, short_leg, net


def analyze_pair(
    a: str,
    b: str,
    benchmark: str = DEFAULT_BENCHMARK,
    capital: float = DEFAULT_CAPITAL,
    months: float = ratio.DEFAULT_MONTHS,
    entry_z: float = DEFAULT_ENTRY_Z,
) -> dict:
    """Build a beta-neutral pair-trade plan for tickers `a` and `b`."""
    pair = ratio.ratio_analysis(a, b, months=months)        # A on B
    a_mkt = ratio.ratio_analysis(a, benchmark, months=months)  # A on benchmark
    b_mkt = ratio.ratio_analysis(b, benchmark, months=months)  # B on benchmark

    at, bt = pair["y"]["ticker"], pair["x"]["ticker"]
    betas = {at: a_mkt["regression"]["beta"], bt: b_mkt["regression"]["beta"]}
    prices = {at: pair["ylast"], bt: pair["xlast"]}

    stats = _ratio_stats(pair["raw"].get("ratioData", {}).get("timeseries", {}))
    corr = pair["correlation"]["last"]
    r2 = pair["regression"].get("rsquared")

    z = stats["z"] or 0.0
    action, long_t, short_t = _signal(z, entry_z, at, bt)

    sizing = None
    if long_t:
        ll, sl, net = _size_beta_neutral(long_t, short_t, betas, prices, capital)
        sizing = {
            "capital": capital,
            "long": ll.__dict__,
            "short": sl.__dict__,
            "net_beta_dollars": round(net, 2),
            "net_beta_pct_of_capital": round(net / capital, 4) if capital else None,
        }

    warnings = []
    if corr is None or corr < MIN_TRADEABLE_R:
        warnings.append(
            f"correlation {corr} < {MIN_TRADEABLE_R}: legs move too independently "
            "for a reliable pair trade")
    if stats["half_life_days"] is None:
        warnings.append("ratio shows no mean reversion (no finite half-life): "
                        "spread may be trending, not reverting")

    return {
        "pair": {"a": at, "b": bt, "benchmark": benchmark},
        "relationship": {
            "correlation": corr,
            "rsquared": r2,
            "hedge_beta_a_on_b": pair["regression"].get("beta"),
        },
        "ratio": {
            "last": pair["ratio"]["last"],
            "mean": stats["mean"],
            "std": stats["std"],
            "z": z,
            "percentile": stats["percentile"],
            "low": pair["ratio"]["low"],
            "high": pair["ratio"]["high"],
            "half_life_days": stats["half_life_days"],
            "entry_z": entry_z,
        },
        "market_beta": {at: betas[at], bt: betas[bt]},
        "signal": action,
        "sizing": sizing,
        "warnings": warnings,
    }
