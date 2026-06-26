"""Portfolio risk analytics: pair Schwab holdings with Massive price history to
produce a quant-grade risk picture — per-position beta/vol/correlation (with
confidence), a full correlation matrix, variance-based risk decomposition,
portfolio beta + the SPY hedge to neutralize it, and concentration.

Per-position beta/vol use each name's own overlap with the benchmark (max data).
The correlation matrix and risk contributions use the common-date intersection
across all priced holdings. Prices are cached in the `prices` table, so repeat
runs are fast.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import prices
from .config import DB_PATH, MARKET_BENCHMARK

TRADING_DAYS = 252
LOW_CONF_R2 = 0.10        # beta is unreliable below this R^2


def _load_closes(symbols: list[str], frm: str, to: str) -> pd.DataFrame:
    """Ensure prices are cached, then return a date x ticker close matrix."""
    prices.ensure_prices(symbols, frm, to, log=lambda *_: None)
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    placeholders = ",".join("?" * len(symbols))
    df = pd.read_sql_query(
        f"SELECT date, ticker, close FROM prices "
        f"WHERE ticker IN ({placeholders}) AND date >= ?",
        conn, params=[*symbols, frm])
    conn.close()
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="date", columns="ticker", values="close").sort_index()


def analyze(snapshot: dict, lookback_days: int = 365) -> dict:
    positions = [p for p in snapshot["positions"]
                 if p.get("asset_type") == "EQUITY" and p.get("market_value")]
    equity = (snapshot["balances"].get("liquidation_value")
              or sum(abs(p["market_value"]) for p in positions) or 1.0)
    symbols = [p["symbol"] for p in positions]

    frm = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to = date.today().strftime("%Y-%m-%d")
    closes = _load_closes(symbols + [MARKET_BENCHMARK], frm, to)
    rets = closes.pct_change().dropna(how="all") if not closes.empty else pd.DataFrame()
    bench = rets[MARKET_BENCHMARK] if MARKET_BENCHMARK in rets else pd.Series(dtype=float)
    bench_var = bench.var()
    spy_last = closes[MARKET_BENCHMARK].dropna().iloc[-1] \
        if MARKET_BENCHMARK in closes and closes[MARKET_BENCHMARK].notna().any() else None

    rows, weights, priced = [], {}, []
    for p in positions:
        sym = p["symbol"]
        mv = p["market_value"]
        w = mv / equity
        beta = r2 = vol = corr = None
        if sym in rets:
            pair = pd.concat([rets[sym], bench], axis=1).dropna()
            if len(pair) >= 30:
                s, m = pair.iloc[:, 0], pair.iloc[:, 1]
                beta = float(s.cov(m) / bench_var) if bench_var else None
                corr = float(s.corr(m))
                r2 = corr ** 2 if corr is not None else None
                vol = float(s.std() * np.sqrt(TRADING_DAYS))
                priced.append(sym)
        weights[sym] = w
        rows.append({
            "symbol": sym, "market_value": round(mv, 2),
            "weight_signed": round(w, 4), "weight_gross": round(abs(w), 4),
            "beta": round(beta, 3) if beta is not None else None,
            "r2": round(r2, 3) if r2 is not None else None,
            "annual_vol": round(vol, 3) if vol is not None else None,
            "corr_spy": round(corr, 3) if corr is not None else None,
            "beta_contribution": round(beta * w, 3) if beta is not None else None,
            "low_confidence": (r2 is not None and r2 < LOW_CONF_R2),
        })

    port_beta = sum(r["beta_contribution"] for r in rows
                    if r["beta_contribution"] is not None)

    # ── portfolio variance / correlation on the common-date intersection ──
    port_vol = div_ratio = None
    corr_block = None
    if len(priced) >= 2:
        sub = rets[priced].dropna()
        if len(sub) >= 30:
            cov_annual = sub.cov() * TRADING_DAYS
            w_vec = np.array([weights[s] for s in priced])
            port_var = float(w_vec @ cov_annual.values @ w_vec)
            port_vol = float(np.sqrt(max(port_var, 0)))
            if port_vol > 0:
                mcr = (cov_annual.values @ w_vec) / port_vol      # marginal
                for r in rows:
                    if r["symbol"] in priced:
                        i = priced.index(r["symbol"])
                        rc = w_vec[i] * mcr[i]
                        r["pct_risk"] = round(rc / port_vol, 3)
                vols = np.sqrt(np.diag(cov_annual.values))
                div_ratio = float((np.abs(w_vec) @ vols) / port_vol)
            cm = sub.corr().round(2)
            corr_block = {"labels": list(cm.columns),
                          "matrix": cm.values.tolist()}

    rows.sort(key=lambda r: r["weight_gross"], reverse=True)
    exposure = snapshot.get("exposure", {})
    hhi = round(sum(r["weight_gross"] ** 2 for r in rows), 3)
    hedge_notional = round(port_beta * equity, 2)

    flags = []
    for r in rows:
        if r["low_confidence"] and abs(r["weight_gross"]) > 0.05:
            flags.append(f"{r['symbol']}: beta unreliable (R²={r['r2']}, "
                         f"corr={r['corr_spy']}) — idiosyncratic, not market-driven")
        if r["annual_vol"] and r["annual_vol"] > 1.5:
            flags.append(f"{r['symbol']}: extreme vol ({r['annual_vol']:.0%} ann.) "
                         "— outlier-driven, treat metrics with caution")

    return {
        "equity": round(equity, 2),
        "benchmark": MARKET_BENCHMARK,
        "lookback_days": lookback_days,
        "days_used": int(len(rets)),
        "portfolio": {
            "beta": round(port_beta, 3),
            "dollar_beta": hedge_notional,
            "hedge_spy_notional": hedge_notional,
            "hedge_spy_shares": (round(hedge_notional / spy_last)
                                 if spy_last else None),
            "annual_vol": round(port_vol, 4) if port_vol else None,
            "diversification_ratio": round(div_ratio, 2) if div_ratio else None,
            "gross_exposure": round(exposure.get("gross", 0), 2),
            "net_exposure": round(exposure.get("net", 0), 2),
            "long": round(exposure.get("long", 0), 2),
            "short": round(exposure.get("short", 0), 2),
            "cash": exposure.get("cash"),
            "n_positions": len(rows),
            "concentration_hhi": hhi,
            "top_position": rows[0]["symbol"] if rows else None,
            "top_weight": rows[0]["weight_gross"] if rows else None,
        },
        "positions": rows,
        "correlations": corr_block,
        "flags": flags,
    }


def _pct(v, dp=1):
    return "  -  " if v is None else f"{v * 100:+.{dp}f}%"


def _num(v, dp=2):
    return "  -  " if v is None else f"{v:.{dp}f}"


def format_report(r: dict) -> str:
    """Human-readable risk report (text)."""
    p = r["portfolio"]
    L = []
    L.append("═" * 72)
    L.append(f"  PORTFOLIO RISK   equity ${r['equity']:,.0f}   "
             f"vs {r['benchmark']}   {r['days_used']} trading days")
    L.append("═" * 72)

    L.append("\n  MARKET EXPOSURE")
    L.append(f"    Beta to {r['benchmark']:<6}   {p['beta']:+.2f}    "
             f"(${p['dollar_beta']:,.0f} market-equivalent exposure)")
    if p["hedge_spy_shares"] is not None:
        side = "SHORT" if p["beta"] > 0 else "LONG"
        L.append(f"    Beta hedge        {side} ${abs(p['hedge_spy_notional']):,.0f} "
                 f"{r['benchmark']} (~{abs(p['hedge_spy_shares'])} sh) to neutralize")
    if p["annual_vol"]:
        L.append(f"    Annual vol        {p['annual_vol'] * 100:.1f}%"
                 + (f"    diversification ratio {p['diversification_ratio']:.2f}"
                    if p["diversification_ratio"] else ""))

    L.append("\n  BOOK")
    L.append(f"    Gross ${p['gross_exposure']:,.0f}   Net ${p['net_exposure']:,.0f}   "
             f"Long ${p['long']:,.0f}   Short ${p['short']:,.0f}"
             + (f"   Cash ${p['cash']:,.0f}" if p["cash"] is not None else ""))
    L.append(f"    {p['n_positions']} positions   HHI {p['concentration_hhi']:.2f}   "
             f"top {p['top_position']} {p['top_weight'] * 100:.0f}%")

    L.append("\n  POSITIONS                          (* = low-confidence beta)")
    L.append(f"    {'SYM':<7}{'WEIGHT':>8}{'BETA':>7}{'R²':>6}{'VOL':>8}"
             f"{'CORR':>7}{'β-CONTR':>9}{'%RISK':>8}")
    L.append("    " + "-" * 60)
    for q in r["positions"]:
        star = "*" if q["low_confidence"] else " "
        L.append(f"    {q['symbol']:<6}{star}{_pct(q['weight_signed'],0):>7}"
                 f"{_num(q['beta']):>7}{_num(q['r2']):>6}{_pct(q['annual_vol'],0):>8}"
                 f"{_num(q['corr_spy']):>7}{_num(q['beta_contribution']):>9}"
                 f"{_pct(q.get('pct_risk'),0):>8}")

    if r["correlations"]:
        labs = r["correlations"]["labels"]
        mat = r["correlations"]["matrix"]
        if len(labs) <= 16:
            L.append("\n  CORRELATION MATRIX")
            L.append("    " + " " * 6 + "".join(f"{l[:5]:>6}" for l in labs))
            for i, lab in enumerate(labs):
                L.append("    " + f"{lab[:5]:<6}"
                         + "".join(f"{mat[i][j]:>6.2f}" for j in range(len(labs))))

    if r["flags"]:
        L.append("\n  ⚠ FLAGS")
        for f in r["flags"]:
            L.append(f"    • {f}")
    L.append("═" * 72)
    return "\n".join(L)
