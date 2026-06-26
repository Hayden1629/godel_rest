"""CLI for the analytics layer.

    python3 -m analytics.run refresh [--min-mentions N] [--delay S]
    python3 -m analytics.run skill [--horizon 5] [--min-calls 8]
    python3 -m analytics.run signals [--horizon 5] [--window 7]
    python3 -m analytics.run buzz
    python3 -m analytics.run clusters
    python3 -m analytics.run influence
    python3 -m analytics.run activity
"""
import argparse
import json

import pandas as pd

from . import clustering, network, pipeline, research, signals, skill, userstats

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def _cmd_fa(a):
    import os
    from . import financials as fa
    if os.path.isfile(a.source):
        df, label = fa.parse_godel_fa(a.source), os.path.basename(a.source)
    else:
        df = fa.fetch_financials(a.source, statement=a.statement, period=a.period)
        label = a.source.upper()
    items = a.items.split(",") if a.items else None
    if a.table:
        print(fa.forecast_table(df, items=items, periods=a.forecast).to_string())
        return
    out = fa.plot_trends(df, items=items, forecast_periods=a.forecast,
                         ticker=label, out_path=a.out)
    print("saved:", out)


def main():
    p = argparse.ArgumentParser(prog="analytics")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh")
    r.add_argument("--min-mentions", type=int, default=2)
    r.add_argument("--delay", type=float, default=0.0)
    r.add_argument("--classifier",
                   choices=["lexicon", "finbert", "ollama", "openrouter",
                            "kimi", "hybrid"],
                   default=None,
                   help="direction labeller (default: keep current backend; "
                        "hybrid uses cheap cloud Kimi by default)")
    r.add_argument("--since-days", type=int, default=None,
                   help="only LLM-classify messages newer than N days "
                        "(older fall back to lexicon)")

    s = sub.add_parser("skill")
    s.add_argument("--horizon", type=int, default=5)
    s.add_argument("--min-calls", type=int, default=8)

    sg = sub.add_parser("signals")
    sg.add_argument("--horizon", type=int, default=5)
    sg.add_argument("--window", type=int, default=7)

    sub.add_parser("buzz")
    sub.add_parser("clusters")
    sub.add_parser("influence")
    sub.add_parser("activity")

    for name in ("price", "fundamentals", "ratios", "dcf", "profile", "chart"):
        sp = sub.add_parser(name)
        sp.add_argument("ticker")

    # FA trends + forecast from a ticker (live API) OR a downloaded xlsx file
    f = sub.add_parser("fa")
    f.add_argument("source", help="ticker (e.g. BSX) or path to a Godel FA .xlsx")
    f.add_argument("--statement", default="income_statement",
                   choices=["income_statement", "balance_sheet", "cash_flow",
                            "cash_flow_statement"])
    f.add_argument("--period", default="QTR", choices=["QTR", "ANN"])
    f.add_argument("--items", default=None, help="comma list, e.g. revenue,net_margin")
    f.add_argument("--forecast", type=int, default=8, help="quarters to forecast")
    f.add_argument("-o", "--out", default=None)
    f.add_argument("--table", action="store_true", help="print forecast table only")
    f.set_defaults(func=lambda a: _cmd_fa(a))

    th = sub.add_parser("thesis")  # full one-ticker research report artifact
    th.add_argument("ticker")
    th.add_argument("--forecast", type=int, default=8)
    th.add_argument("--lookback", type=int, default=365)
    th.add_argument("--out", default="output")

    # full deterministic model: download 3 statements + charts + EV reverse DCF
    md = sub.add_parser("model")
    md.add_argument("ticker")
    md.add_argument("--period", default="QTR", choices=["QTR", "ANN"])
    md.add_argument("--forecast", type=int, default=8,
                    help="quarters to forecast on the trend charts")
    md.add_argument("--discount-rate", type=float, default=0.10)
    md.add_argument("--terminal-growth", type=float, default=0.025)
    md.add_argument("--years", type=int, default=10)
    md.add_argument("--out", default="output")

    a = p.parse_args()
    if a.cmd == "refresh":
        pipeline.refresh(min_ticker_mentions=a.min_mentions, price_delay=a.delay,
                         classifier=a.classifier, since_days=a.since_days)
    elif a.cmd == "skill":
        print("summary:", skill.summary(a.horizon))
        print(skill.leaderboard(a.horizon, a.min_calls).head(30).to_string(index=False))
    elif a.cmd == "signals":
        print(signals.smart_sentiment(a.horizon, a.window).head(30).to_string(index=False))
    elif a.cmd == "buzz":
        print(signals.recent_buzz().head(30).to_string(index=False))
    elif a.cmd == "clusters":
        c = clustering.user_clusters()
        print(clustering.cluster_themes(c).to_string(index=False))
    elif a.cmd == "influence":
        nodes, _ = network.influence()
        print(nodes.head(30).to_string(index=False))
    elif a.cmd == "activity":
        print(userstats.overview())
        print(userstats.top_senders(limit=20).to_string(index=False))
    elif a.cmd == "price":
        print(json.dumps(research.price_summary(a.ticker), indent=2))
    elif a.cmd == "fundamentals":
        print(json.dumps(research.fundamentals(a.ticker), indent=2, default=str))
    elif a.cmd == "ratios":
        print(json.dumps(research.ratios(a.ticker), indent=2))
    elif a.cmd == "dcf":
        print(json.dumps(research.reverse_dcf(a.ticker), indent=2))
    elif a.cmd == "profile":
        print(json.dumps(research.profile(a.ticker), indent=2, default=str))
    elif a.cmd == "chart":
        print("saved:", research.save_price_chart(a.ticker))
    elif a.cmd == "fa":
        _cmd_fa(a)
    elif a.cmd == "model":
        from . import model as model_mod
        res = model_mod.run(a.ticker, period=a.period, forecast=a.forecast,
                            discount_rate=a.discount_rate,
                            terminal_growth=a.terminal_growth, years=a.years,
                            outdir=a.out)
        v, rd = res["valuation"], res["reverse_dcf"]
        print(f"\n=== {res['ticker']} model ===")
        print(f"price ${v['price']}  shares {v['shares_outstanding']:,}"
              if v.get("shares_outstanding") else f"price ${v['price']}")
        if v.get("market_cap"):
            print(f"market cap ${v['market_cap']/1e9:,.1f}B  "
                  f"net debt ${(v['net_debt'] or 0)/1e3:,.1f}B  "
                  f"EV ${(v['enterprise_value'] or 0)/1e9:,.1f}B")
        print(f"TTM FCF ${(v['fcf_ttm'] or 0)/1e3:,.2f}B")
        if "error" in rd:
            print("reverse DCF:", rd["error"])
        else:
            print(f"EV/FCF {rd['ev_to_fcf']}x  implied FCF growth "
                  f"{rd['implied_fcf_growth']:.1%}  (hist "
                  f"{rd['historical_fcf_cagr']:.1%})"
                  if rd.get("historical_fcf_cagr") is not None
                  else f"EV/FCF {rd['ev_to_fcf']}x  implied FCF growth "
                       f"{rd['implied_fcf_growth']:.1%}")
            print(rd["read"])
        print("chart:", res["chart"])
        print("files:", len(res["files"]), "saved in", a.out)
    elif a.cmd == "thesis":
        from . import thesis
        res = thesis.build_report(a.ticker, forecast_q=a.forecast,
                                  lookback_days=a.lookback, out_dir=a.out)
        print("artifact:", res["html"])


if __name__ == "__main__":
    main()
