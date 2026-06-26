"""Rudimentary single-stock modeling on top of the Massive API.

Functions are deliberately small and text-first so an agent can call them and
reason over the output:

    price_summary    momentum, drawdown, moving averages, 52w range
    save_price_chart writes a PNG price+SMA chart
    fundamentals     revenue/income/FCF trend + latest snapshot
    ratios           valuation, profitability, growth, leverage
    reverse_dcf      the FCF growth rate the market is implying
    profile          all of the above in one dict

Caveats: normalized financials don't break out capex, so FCF is approximated by
operating cash flow. Reverse DCF discounts FCF to equity (uses market cap, not
EV) — a rough "what's priced in" gauge, not a precise valuation.
"""
from datetime import date, timedelta

from . import massive, prices


# ── prices / technicals ───────────────────────────────────────────────────────

def _bars(ticker: str, lookback_days: int = 400) -> list[dict]:
    frm = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to = date.today().strftime("%Y-%m-%d")
    return prices.fetch_bars(ticker, frm, to)


def price_summary(ticker: str, lookback_days: int = 400) -> dict:
    bars = _bars(ticker, lookback_days)
    if not bars:
        return {"ticker": ticker, "error": "no price data"}
    closes = [b["c"] for b in bars]
    last = closes[-1]

    def ret(n):
        return (last / closes[-1 - n] - 1) if len(closes) > n else None

    def sma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    hi = max(closes)
    lo = min(closes)
    # max drawdown over the window
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    return {
        "ticker": ticker,
        "last": round(last, 2),
        "ret_1w": ret(5), "ret_1m": ret(21), "ret_3m": ret(63),
        "ret_6m": ret(126), "ret_1y": ret(252),
        "sma_20": round(sma(20), 2) if sma(20) else None,
        "sma_50": round(sma(50), 2) if sma(50) else None,
        "sma_200": round(sma(200), 2) if sma(200) else None,
        "above_sma_200": (last > sma(200)) if sma(200) else None,
        "high_52w": round(hi, 2), "low_52w": round(lo, 2),
        "pct_from_high": round(last / hi - 1, 4),
        "max_drawdown": round(mdd, 4),
        "bars": len(bars),
    }


def save_price_chart(ticker: str, out_path: str | None = None,
                     lookback_days: int = 400) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime

    bars = _bars(ticker, lookback_days)
    if not bars:
        raise RuntimeError(f"no price data for {ticker}")
    xs = [datetime.utcfromtimestamp(b["t"] / 1000) for b in bars]
    closes = [b["c"] for b in bars]

    def sma(n):
        return [None] * (n - 1) + [sum(closes[i - n + 1:i + 1]) / n
                                   for i in range(n - 1, len(closes))]

    out_path = out_path or f"output/{ticker}_chart.png"
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    plt.figure(figsize=(11, 5))
    plt.plot(xs, closes, label="close", linewidth=1.3)
    if len(closes) >= 50:
        plt.plot(xs, sma(50), label="SMA50", linewidth=0.9)
    if len(closes) >= 200:
        plt.plot(xs, sma(200), label="SMA200", linewidth=0.9)
    plt.title(f"{ticker} — {lookback_days}d")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=110)
    plt.close()
    return out_path


# ── fundamentals ──────────────────────────────────────────────────────────────

def _g(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def fundamentals(ticker: str, years: int = 5) -> dict:
    fins = massive.financials(ticker, timeframe="annual", limit=years)
    details = massive.ticker_details(ticker)
    trend = []
    for f in fins:
        inc, cf = f["income"], f["cash_flow"]
        rev = _g(inc, "revenues")
        ni = _g(inc, "net_income_loss")
        ocf = _g(cf, "net_cash_flow_from_operating_activities")
        trend.append({
            "year": f["fiscal_year"], "period_end": f["end_date"],
            "revenue": rev,
            "gross_profit": _g(inc, "gross_profit"),
            "operating_income": _g(inc, "operating_income_loss"),
            "net_income": ni,
            "diluted_eps": _g(inc, "diluted_earnings_per_share",
                              "basic_earnings_per_share"),
            "operating_cash_flow": ocf,
            "net_margin": (ni / rev) if rev else None,
        })
    return {"details": details, "annual": trend, "latest": trend[0] if trend else None}


# ── ratios ────────────────────────────────────────────────────────────────────

def _cagr(series) -> float | None:
    vals = [v for v in series if v not in (None, 0)]
    if len(vals) < 2 or vals[0] is None or vals[-1] is None:
        return None
    first, last = vals[-1], vals[0]  # series is newest-first
    if first <= 0 or last <= 0:
        return None
    n = len(vals) - 1
    return (last / first) ** (1 / n) - 1


def ratios(ticker: str) -> dict:
    fund = fundamentals(ticker)
    d = fund["details"]
    annual = fund["annual"]
    if not annual:
        return {"ticker": ticker, "error": "no fundamentals"}
    latest = annual[0]
    mcap = d.get("market_cap")
    shares = d.get("shares_outstanding")
    rev = latest["revenue"]
    ni = latest["net_income"]
    ocf = latest["operating_cash_flow"]
    bs = massive.financials(ticker, "annual", 1)
    balance = bs[0]["balance"] if bs else {}
    equity = _g(balance, "equity", "equity_attributable_to_parent")
    assets = _g(balance, "assets")
    cur_assets = _g(balance, "current_assets")
    cur_liab = _g(balance, "current_liabilities")
    liabilities = _g(balance, "liabilities")

    def safe(n, dd):
        return (n / dd) if (n is not None and dd not in (None, 0)) else None

    return {
        "ticker": ticker, "name": d.get("name"), "sector": d.get("sector"),
        "market_cap": mcap,
        "valuation": {
            "pe": safe(mcap, ni),
            "ps": safe(mcap, rev),
            "p_ocf": safe(mcap, ocf),
            "p_b": safe(mcap, equity),
        },
        "profitability": {
            "gross_margin": safe(latest["gross_profit"], rev),
            "operating_margin": safe(latest["operating_income"], rev),
            "net_margin": safe(ni, rev),
            "roe": safe(ni, equity),
            "roa": safe(ni, assets),
        },
        "growth_cagr": {
            "revenue": _cagr([a["revenue"] for a in annual]),
            "net_income": _cagr([a["net_income"] for a in annual]),
            "ocf": _cagr([a["operating_cash_flow"] for a in annual]),
        },
        "leverage": {
            "debt_to_equity": safe(liabilities, equity),
            "current_ratio": safe(cur_assets, cur_liab),
        },
    }


# ── reverse DCF ───────────────────────────────────────────────────────────────

def _dcf_value(fcf, g, r, tg, years):
    pv = 0.0
    cf = fcf
    for t in range(1, years + 1):
        cf = fcf * (1 + g) ** t
        pv += cf / (1 + r) ** t
    terminal = cf * (1 + tg) / (r - tg)
    pv += terminal / (1 + r) ** years
    return pv


def reverse_dcf(ticker: str, discount_rate: float = 0.10,
                terminal_growth: float = 0.025, years: int = 10) -> dict:
    fund = fundamentals(ticker)
    d = fund["details"]
    annual = fund["annual"]
    mcap = d.get("market_cap")
    fcf = annual[0]["operating_cash_flow"] if annual else None
    if not (mcap and fcf and fcf > 0):
        return {"ticker": ticker,
                "error": "need positive FCF (op cash flow) and market cap"}

    lo, hi = -0.5, 1.0
    target = mcap
    for _ in range(100):
        mid = (lo + hi) / 2
        val = _dcf_value(fcf, mid, discount_rate, terminal_growth, years)
        if val > target:
            hi = mid
        else:
            lo = mid
    implied = (lo + hi) / 2
    hist = _cagr([a["operating_cash_flow"] for a in annual])
    return {
        "ticker": ticker, "name": d.get("name"),
        "market_cap": mcap, "fcf_proxy_ocf": fcf,
        "assumptions": {"discount_rate": discount_rate,
                        "terminal_growth": terminal_growth, "years": years},
        "implied_fcf_growth": round(implied, 4),
        "historical_ocf_cagr": round(hist, 4) if hist is not None else None,
        "read": _dcf_read(implied, hist),
    }


def _dcf_read(implied, hist):
    if hist is None:
        return (f"Market prices in ~{implied:.1%} annual FCF growth "
                "(no positive cash-flow history to compare against — early/"
                "unprofitable, so treat with caution).")
    if implied > hist + 0.02:
        return (f"Market prices in {implied:.1%} growth vs {hist:.1%} historical "
                "— priced for acceleration (demanding).")
    if implied < hist - 0.02:
        return (f"Market prices in {implied:.1%} growth vs {hist:.1%} historical "
                "— low bar (potentially cheap if trend holds).")
    return (f"Market prices in {implied:.1%} growth, ~in line with {hist:.1%} "
            "historical.")


def profile(ticker: str) -> dict:
    """Everything in one call for an agent report."""
    out = {"ticker": ticker}
    for name, fn in (("details", lambda: massive.ticker_details(ticker)),
                     ("price", lambda: price_summary(ticker)),
                     ("ratios", lambda: ratios(ticker)),
                     ("reverse_dcf", lambda: reverse_dcf(ticker))):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)}
    return out
