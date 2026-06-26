"""Portfolio-fit analysis: how a candidate ticker relates to your existing
holdings. Quantifies correlation/beta to each position and to the book as a
whole, the marginal risk of adding it, and whether it diversifies, concentrates,
or could hedge — the quant inputs an agent combines with its directional view to
decide long / short / pass.

Read-only: it never trades.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import prices
from .config import MARKET_BENCHMARK


def _returns(symbols: list[str], frm: str, to: str) -> pd.DataFrame:
    prices.ensure_prices(symbols, frm, to, log=lambda *_: None)
    import sqlite3
    from .config import DB_PATH
    conn = sqlite3.connect(str(DB_PATH))
    ph = ",".join("?" * len(symbols))
    df = pd.read_sql_query(
        f"SELECT date, ticker, close FROM prices WHERE ticker IN ({ph}) AND date>=?",
        conn, params=[*symbols, frm])
    conn.close()
    if df.empty:
        return pd.DataFrame()
    closes = df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    return closes.pct_change().dropna(how="all")


def analyze_fit(candidate: str, holdings: list[dict], equity: float | None = None,
                lookback_days: int = 365) -> dict:
    """`holdings` = [{symbol, market_value}]. Returns correlation/beta of the
    candidate to each holding and to the whole portfolio, plus a fit verdict."""
    candidate = candidate.upper()
    held = [h for h in holdings if (h.get("symbol") or "").upper() != candidate
            and h.get("market_value")]
    syms = [h["symbol"] for h in held]
    equity = equity or sum(abs(h["market_value"]) for h in held) or 1.0

    frm = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to = date.today().strftime("%Y-%m-%d")
    rets = _returns([candidate] + syms + [MARKET_BENCHMARK], frm, to)
    if candidate not in rets:
        return {"candidate": candidate, "error": "no price data for candidate"}

    cand = rets[candidate]
    # weighted portfolio return series (signed by dollar exposure / equity)
    w = {h["symbol"]: h["market_value"] / equity for h in held if h["symbol"] in rets}
    port = sum(rets[s] * wt for s, wt in w.items())
    pair = pd.concat([cand, port], axis=1).dropna()
    pair.columns = ["c", "p"]
    corr_port = float(pair["c"].corr(pair["p"])) if len(pair) >= 30 else None

    per_holding = []
    for h in held:
        s = h["symbol"]
        if s not in rets:
            continue
        d = pd.concat([cand, rets[s]], axis=1).dropna()
        if len(d) < 30:
            continue
        per_holding.append({
            "symbol": s,
            "weight": round(h["market_value"] / equity, 4),
            "corr": round(float(d.iloc[:, 0].corr(d.iloc[:, 1])), 2),
        })
    per_holding.sort(key=lambda x: x["corr"], reverse=True)

    # beta of candidate to SPY and to the portfolio
    def beta_to(series):
        d = pd.concat([cand, series], axis=1).dropna()
        v = d.iloc[:, 1].var()
        return float(d.iloc[:, 0].cov(d.iloc[:, 1]) / v) if v and len(d) >= 30 else None

    beta_spy = beta_to(rets[MARKET_BENCHMARK]) if MARKET_BENCHMARK in rets else None
    cand_vol = float(cand.std() * np.sqrt(252))

    # marginal vol impact of adding the candidate at a 5% weight
    add_w = 0.05
    port_vol = float(port.std() * np.sqrt(252)) if len(port) else None
    new_vol = None
    if port_vol is not None:
        combo = (1 - add_w) * port + add_w * cand
        new_vol = float(combo.std() * np.sqrt(252))

    avg_corr = (np.mean([h["corr"] for h in per_holding])
                if per_holding else None)
    verdict = _verdict(corr_port, avg_corr, port_vol, new_vol)

    return {
        "candidate": candidate,
        "days": int(len(rets)),
        "corr_to_portfolio": corr_port,
        "avg_corr_to_holdings": round(float(avg_corr), 2) if avg_corr is not None else None,
        "beta_to_spy": round(beta_spy, 2) if beta_spy is not None else None,
        "candidate_annual_vol": round(cand_vol, 3),
        "portfolio_vol_now": round(port_vol, 4) if port_vol else None,
        "portfolio_vol_with_5pct": round(new_vol, 4) if new_vol else None,
        "vol_delta_5pct": (round(new_vol - port_vol, 4)
                           if (new_vol is not None and port_vol is not None) else None),
        "most_correlated": per_holding[:5],
        "least_correlated": per_holding[-5:][::-1],
        "fit": verdict,
    }


def _verdict(corr_port, avg_corr, port_vol, new_vol) -> dict:
    """Quant-only classification; the directional long/short call is left to the
    agent's view combined with this."""
    if corr_port is None:
        return {"class": "unknown", "note": "insufficient overlapping history"}
    if corr_port < 0.2:
        cls = "diversifier"
        note = ("low correlation to the book — adds largely independent risk; "
                "a LONG here improves diversification if your thesis is positive.")
    elif corr_port < 0.5:
        cls = "moderate"
        note = ("partial correlation — some diversification, but it moves with the "
                "book in stress.")
    else:
        cls = "concentrator"
        note = ("highly correlated with existing holdings — a LONG mostly doubles "
                "down on current exposure; only add on high conviction, or "
                "consider it (or a correlated name) as a SHORT hedge.")
    if port_vol and new_vol and new_vol < port_vol:
        note += " Adding 5% would actually LOWER portfolio vol."
    return {"class": cls, "note": note}
