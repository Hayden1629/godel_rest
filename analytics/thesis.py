"""One-ticker research framework: financials + forecasts + price risk +
valuation + chat intelligence → a single self-contained HTML thesis (and a
markdown copy). Deterministic for a given DB / market snapshot: same inputs
produce the same report.

    python3 -m analytics.run thesis BE
    from analytics.thesis import build_report; build_report("BE")
"""
import base64
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import financials, massive, prices, research, skill
from .config import DB_PATH, MARKET_BENCHMARK

TRADING_DAYS = 252


# ── price risk ────────────────────────────────────────────────────────────────

def price_risk(ticker: str, lookback_days: int = 365) -> dict:
    frm = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to = date.today().strftime("%Y-%m-%d")

    def rets(sym):
        bars = prices.fetch_bars(sym, frm, to)
        s = pd.Series({b["t"]: b["c"] for b in bars if b.get("c")}).sort_index()
        return s.pct_change().dropna(), s

    r, closes = rets(ticker)
    m, _ = rets(MARKET_BENCHMARK)
    pair = pd.concat([r, m], axis=1).dropna()
    pair.columns = ["s", "m"]
    beta = corr = r2 = None
    if len(pair) >= 30 and pair["m"].var():
        beta = float(pair["s"].cov(pair["m"]) / pair["m"].var())
        corr = float(pair["s"].corr(pair["m"]))
        r2 = corr ** 2
    ann_vol = float(r.std() * np.sqrt(TRADING_DAYS)) if len(r) else None
    daily_std = float(r.std()) if len(r) else None
    ann_ret = float(r.mean() * TRADING_DAYS) if len(r) else None
    sharpe = (ann_ret / ann_vol) if (ann_vol and ann_vol > 0) else None
    downside = r[r < 0].std() * np.sqrt(TRADING_DAYS) if len(r) else None
    sortino = (ann_ret / downside) if (downside and downside > 0) else None
    # max drawdown
    mdd = None
    if len(closes):
        peak = closes.cummax()
        mdd = float((closes / peak - 1).min())
    return {
        "last": float(closes.iloc[-1]) if len(closes) else None,
        "beta": beta, "corr_spy": corr, "r2": r2,
        "annual_vol": ann_vol, "daily_std": daily_std,
        "annual_return": ann_ret, "sharpe": sharpe, "sortino": sortino,
        "max_drawdown": mdd, "days": int(len(r)),
        "low_confidence_beta": (r2 is not None and r2 < 0.10),
    }


# ── chat intelligence ─────────────────────────────────────────────────────────

def chat_intel(ticker: str) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    base = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT user_id) users, MIN(ts) first, MAX(ts) last "
        "FROM events WHERE ticker=?", (ticker,)).fetchone()
    if not base or base["n"] == 0:
        conn.close()
        return {"mentions": 0}
    dirs = {r["direction"]: r["c"] for r in conn.execute(
        "SELECT direction, COUNT(*) c FROM events WHERE ticker=? GROUP BY direction",
        (ticker,))}
    perf = conn.execute(
        """SELECT AVG(r.abn_5d) avg_abn,
                  AVG(CASE WHEN e.direction*r.abn_5d>0 THEN 1.0 ELSE 0 END) hit
           FROM events e JOIN event_returns r
             ON r.message_id=e.message_id AND r.series_id=e.series_id
           WHERE e.ticker=? AND e.direction!=0 AND r.abn_5d IS NOT NULL""",
        (ticker,)).fetchone()
    top_users = conn.execute(
        "SELECT user_id, username, COUNT(*) n, "
        "SUM(CASE WHEN direction>0 THEN 1 ELSE 0 END) bull, "
        "SUM(CASE WHEN direction<0 THEN 1 ELSE 0 END) bear "
        "FROM events WHERE ticker=? GROUP BY user_id ORDER BY n DESC LIMIT 8",
        (ticker,)).fetchall()
    recent = conn.execute(
        """SELECT m.created_at, m.username, m.content
           FROM chat_tickers t JOIN chat_messages m ON m.id=t.message_id
           WHERE t.ticker=? ORDER BY m.id DESC LIMIT 6""", (ticker,)).fetchall()
    # recent 30d net lean — anchored to the data's last mention (deterministic)
    anchor = datetime.fromisoformat((base["last"] or "2000-01-01")[:19])
    cutoff = (anchor - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_dir = conn.execute(
        "SELECT SUM(direction) net, COUNT(*) n FROM events "
        "WHERE ticker=? AND substr(ts,1,10)>=? AND direction!=0",
        (ticker, cutoff)).fetchone()
    conn.close()

    lb = skill.leaderboard(horizon=5, min_calls=5)
    skill_map = {r.user_id: r for r in lb.itertuples()} if not lb.empty else {}
    users = []
    for u in top_users:
        sk = skill_map.get(u["user_id"])
        users.append({
            "username": u["username"], "n": u["n"],
            "bull": u["bull"], "bear": u["bear"],
            "skill_rank": int(sk.rank) if sk is not None else None,
            "skill_hit": float(sk.hit_rate) if sk is not None else None,
            "skill_edge": float(sk.mean_shrunk) if sk is not None else None,
        })
    return {
        "mentions": base["n"], "users": base["users"],
        "first": base["first"], "last": base["last"],
        "bull": dirs.get(1, 0), "bear": dirs.get(-1, 0), "neutral": dirs.get(0, 0),
        "fwd_avg_abn_5d": perf["avg_abn"], "fwd_hit_5d": perf["hit"],
        "recent_30d_net": recent_dir["net"], "recent_30d_n": recent_dir["n"],
        "top_users": users,
        "recent_messages": [dict(r) for r in recent],
    }


# ── assembly ──────────────────────────────────────────────────────────────────

def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def gather(ticker: str, forecast_q: int = 8, lookback_days: int = 365,
           out_dir: str = "output") -> dict:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ticker = ticker.upper()
    data = {"ticker": ticker, "as_of": date.today().isoformat()}
    data["details"] = _safe(lambda: massive.ticker_details(ticker), {})
    data["price"] = _safe(lambda: research.price_summary(ticker), {})
    data["risk"] = _safe(lambda: price_risk(ticker, lookback_days), {})
    data["ratios"] = _safe(lambda: research.ratios(ticker), {})
    data["dcf"] = _safe(lambda: research.reverse_dcf(ticker), {})
    data["chat"] = _safe(lambda: chat_intel(ticker), {"mentions": 0})

    # financials + forecast
    inc = _safe(lambda: financials.fetch_financials(ticker, "income_statement"), None)
    data["income"] = inc
    if inc is not None and not inc.empty:
        data["forecast"] = financials.forecast_table(inc, periods=forecast_q)
        data["fin_chart"] = financials.plot_trends(
            inc, ticker=ticker, forecast_periods=forecast_q,
            out_path=f"{out_dir}/{ticker}_financials.png")
    data["price_chart"] = _safe(
        lambda: research.save_price_chart(ticker, out_path=f"{out_dir}/{ticker}_price.png"),
        None)
    return data


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        return default if not isinstance(default, dict) else {**default, "error": str(e)}


# ── formatting helpers ────────────────────────────────────────────────────────

def _pct(v, dp=1):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:+.{dp}f}%"


def _money(v):
    if v is None:
        return "—"
    a = abs(v)
    for unit, d in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if a >= d:
            return f"${v / d:.2f}{unit}"
    return f"${v:,.0f}"


def _num(v, dp=2):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{dp}f}"


# ── synthesis (rule-based thesis bullets) ─────────────────────────────────────

def _synthesize(d: dict) -> list[str]:
    out = []
    r, ratios, dcf, chat = d["risk"], d["ratios"], d["dcf"], d["chat"]
    val = (ratios or {}).get("valuation", {})
    growth = (ratios or {}).get("growth_cagr", {})
    inc = d.get("income")

    if dcf and dcf.get("read"):
        out.append(f"**Valuation** — {dcf['read']}")
    elif val.get("ps"):
        out.append(f"**Valuation** — trades at {_num(val['ps'])}x sales"
                   + (f", {_num(val['pe'])}x earnings" if val.get("pe") else
                      " (no positive earnings)") + ".")

    if inc is not None and not inc.empty and "net_margin" in inc:
        nm = inc["net_margin"].dropna()
        if len(nm):
            trend = "improving" if len(nm) > 4 and nm.iloc[-1] > nm.iloc[-5] else "soft/declining"
            out.append(f"**Profitability** — last net margin {_pct(nm.iloc[-1])}, {trend} "
                       f"vs a year ago.")
    if growth.get("revenue") is not None:
        out.append(f"**Growth** — revenue compounding ~{_pct(growth['revenue'])}/yr "
                   "(historical).")

    if r.get("beta") is not None:
        risk_word = ("very high" if r["beta"] > 2 else "high" if r["beta"] > 1.3
                     else "market-like" if r["beta"] > 0.7 else "low")
        conf = " (but low R² — idiosyncratic, not truly market-driven)" \
            if r.get("low_confidence_beta") else ""
        out.append(f"**Risk** — beta {_num(r['beta'])} ({risk_word}){conf}; "
                   f"{_pct(r.get('annual_vol'),0)} annualized vol, "
                   f"max drawdown {_pct(r.get('max_drawdown'),0)}.")

    if chat.get("mentions"):
        lean = chat["bull"] - chat["bear"]
        lean_word = "net bullish" if lean > 0 else "net bearish" if lean < 0 else "mixed"
        line = (f"**Chat** — {chat['mentions']} mentions by {chat['users']} users, "
                f"{lean_word} ({chat['bull']}▲/{chat['bear']}▼).")
        if chat.get("fwd_hit_5d") is not None:
            line += (f" Past directional calls were right "
                     f"{_pct(chat['fwd_hit_5d'],0)} of the time (5d, "
                     f"avg {_pct(chat.get('fwd_avg_abn_5d'))} abnormal).")
        out.append(line)
    return out


# ── markdown / html rendering ─────────────────────────────────────────────────

def _positions_md(d: dict) -> str:
    det, price, r = d["details"], d["price"], d["risk"]
    val = (d["ratios"] or {}).get("valuation", {})
    prof = (d["ratios"] or {}).get("profitability", {})
    rows = [
        ("Last price", _num(price.get("last")) if price.get("last") else "—"),
        ("Market cap", _money(det.get("market_cap"))),
        ("Sector", det.get("sector") or "—"),
        ("Beta (1y)", _num(r.get("beta")) + (" *low R²" if r.get("low_confidence_beta") else "")),
        ("Annual vol", _pct(r.get("annual_vol"), 0)),
        ("Daily std", _pct(r.get("daily_std"), 2)),
        ("Sharpe (rf=0)", _num(r.get("sharpe"))),
        ("Max drawdown", _pct(r.get("max_drawdown"), 0)),
        ("1y return", _pct(price.get("ret_1y"), 0)),
        ("P/E", _num(val.get("pe"))), ("P/S", _num(val.get("ps"))),
        ("Net margin", _pct(prof.get("net_margin"))),
        ("Implied FCF growth", _pct((d["dcf"] or {}).get("implied_fcf_growth"))),
        ("Hist. OCF CAGR", _pct((d["dcf"] or {}).get("historical_ocf_cagr"))),
    ]
    return "\n".join(f"| {k} | {v} |" for k, v in rows)


def _forecast_md(d: dict) -> str:
    fc = d.get("forecast")
    if fc is None or fc.empty:
        return "_No financial data._"
    items = [c for c in ["revenue", "net_income", "gross_margin",
                         "operating_margin", "net_margin"] if c in fc.columns]
    head = "| Quarter | " + " | ".join(items) + " |"
    sep = "|" + "---|" * (len(items) + 1)
    lines = [head, sep]
    for idx, row in fc.iterrows():
        cells = []
        for it in items:
            v = row[it]
            cells.append(_pct(v) if "margin" in it else f"{v:,.0f}")
        lines.append(f"| {idx:%Y-%m} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _chat_md(d: dict) -> str:
    c = d["chat"]
    if not c.get("mentions"):
        return "_No chat mentions found._"
    lines = [f"**{c['mentions']} mentions** by **{c['users']} users** "
             f"({c['first'][:10]} → {c['last'][:10]})  |  "
             f"{c['bull']}▲ bull / {c['bear']}▼ bear / {c['neutral']}· neutral",
             ""]
    if c.get("fwd_hit_5d") is not None:
        lines.append(f"Forward 5d on directional calls: hit rate "
                     f"{_pct(c['fwd_hit_5d'],0)}, avg abnormal {_pct(c.get('fwd_avg_abn_5d'))}.")
        lines.append("")
    lines.append("**Most active on this name:**")
    lines.append("| User | Mentions | ▲/▼ | Skill rank | Hit | Edge |")
    lines.append("|---|---|---|---|---|---|")
    for u in c["top_users"]:
        rank = f"#{u['skill_rank']}" if u["skill_rank"] else "—"
        hit = _pct(u["skill_hit"], 0) if u["skill_hit"] is not None else "—"
        edge = _pct(u["skill_edge"]) if u["skill_edge"] is not None else "—"
        lines.append(f"| {u['username']} | {u['n']} | {u['bull']}/{u['bear']} "
                     f"| {rank} | {hit} | {edge} |")
    lines.append("\n**Recent messages:**\n")
    for m in c["recent_messages"]:
        txt = m["content"].replace("\n", " ")[:160]
        lines.append(f"> *{m['created_at'][:16]}* **{m['username']}**: {txt}")
    return "\n".join(lines)


def render_markdown(d: dict) -> str:
    det = d["details"]
    name = det.get("name") or d["ticker"]
    bullets = "\n".join(f"- {b}" for b in _synthesize(d))
    md = f"""# {d['ticker']} — {name}
*Research thesis · generated {d['as_of']} · benchmark {MARKET_BENCHMARK}*

## Thesis
{bullets or '_insufficient data_'}

## Snapshot
| Metric | Value |
|---|---|
{_positions_md(d)}

## Price & risk
![price]({d['ticker']}_price.png)

## Financial trends & {len(d.get('forecast', [])) if d.get('forecast') is not None else 0}-quarter forecast
![financials]({d['ticker']}_financials.png)

### Forecast
{_forecast_md(d)}

## Chat intelligence
{_chat_md(d)}

---
*Deterministic framework output. Direction labels are model-derived; chat
metrics reflect predictive-of-price, not realized P&L. Not investment advice.*
"""
    return md


def render_html(d: dict, out_dir: str) -> str:
    import markdown as _md  # optional
    md = render_markdown(d)
    # embed charts as base64 so the HTML is one self-contained file
    for tag, key in (("price", "price_chart"), ("financials", "fin_chart")):
        path = d.get(key)
        png = f"{d['ticker']}_{tag}.png"
        if path and Path(path).exists():
            md = md.replace(f"({png})", f"(data:image/png;base64,{_b64(path)})")
    try:
        body = _md.markdown(md, extensions=["tables"])
    except Exception:
        body = "<pre>" + md.replace("<", "&lt;") + "</pre>"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{d['ticker']} thesis</title><style>
body{{font:15px/1.6 -apple-system,Segoe UI,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#1a1a1a}}
h1{{border-bottom:3px solid #1f77b4;padding-bottom:.2em}}
h2{{margin-top:1.6em;color:#1f77b4;border-bottom:1px solid #eee}}
table{{border-collapse:collapse;margin:1em 0;font-size:14px}}
td,th{{border:1px solid #ddd;padding:5px 10px;text-align:left}}
th{{background:#f5f7fa}} img{{max-width:100%;border:1px solid #eee;border-radius:6px}}
blockquote{{border-left:3px solid #ccc;margin:.4em 0;padding:.2em .8em;color:#555;font-size:13px}}
code{{background:#f3f3f3;padding:1px 4px}}</style></head><body>{body}</body></html>"""


def build_report(ticker: str, forecast_q: int = 8, lookback_days: int = 365,
                 out_dir: str = "output", log=print) -> dict:
    log(f"[thesis] building {ticker.upper()} …")
    d = gather(ticker, forecast_q, lookback_days, out_dir)
    ticker = d["ticker"]
    md_path = Path(out_dir) / f"{ticker}_thesis.md"
    html_path = Path(out_dir) / f"{ticker}_thesis.html"
    md_path.write_text(render_markdown(d))
    html_path.write_text(render_html(d, out_dir))
    log(f"[thesis] wrote {html_path} and {md_path}")
    return {"html": str(html_path), "md": str(md_path),
            "charts": [d.get("price_chart"), d.get("fin_chart")]}

