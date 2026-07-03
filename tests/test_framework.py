"""Deterministic validation of the research framework (no LLM required).

    python3 -m tests.test_framework

Exits non-zero if any check fails. The agent end-to-end test (LLM-driven) lives
separately in agent/ and is exercised via `python3 -m agent.run "<task>"`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok
    FAIL += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    print("financials (Gödel FA API)")
    from analytics import financials as fa
    df = fa.fetch_financials("AAPL")
    check("fetch income statement", not df.empty and "revenue" in df.columns,
          f"{df.shape}")
    check("margins computed", "net_margin" in df.columns)
    ft = fa.forecast_table(df, items=["revenue", "net_margin"], periods=4)
    check("forecast produced", len(ft) == 4 and "revenue" in ft.columns)

    print("valuation / risk modeling")
    from analytics import research
    rt = research.ratios("AAPL")
    check("ratios has P/E", rt.get("valuation", {}).get("pe") is not None)
    pr = research.price_summary("AAPL")
    check("price summary", pr.get("last") is not None)

    print("portfolio fit (synthetic holdings, real prices)")
    from analytics import portfolio_fit
    holds = [{"symbol": "MU", "market_value": 5000},
             {"symbol": "NVDA", "market_value": 5000}]
    fit = portfolio_fit.analyze_fit("AMD", holds, equity=10000)
    check("fit returns correlation", fit.get("corr_to_portfolio") is not None,
          f"corr={fit.get('corr_to_portfolio')}")
    check("fit verdict present", "class" in fit.get("fit", {}))

    print("thesis artifact + determinism")
    from analytics import thesis
    res = thesis.build_report("AAPL", out_dir="output", log=lambda *_: None)
    html, md = Path(res["html"]), Path(res["md"])
    check("html artifact written", html.exists() and html.stat().st_size > 10000)
    check("self-contained (base64 charts)", "data:image/png;base64" in html.read_text())
    first = md.read_text()
    thesis.build_report("AAPL", out_dir="output", log=lambda *_: None)
    check("deterministic (identical md on rerun)", md.read_text() == first)

    print("ratio analysis (Gödel ratio-analysis API)")
    from server import ratio as ratio_mod
    s = ratio_mod.resolve_series("AMD")
    check("resolve ticker -> series", s.series_id == 4678082, f"{s.series_id}")
    rr = ratio_mod.ratio_analysis("AMD", "SOX", months=6)
    reg = rr.get("regression", {})
    check("regression has beta/rsquared/stderr",
          all(k in reg for k in ("beta", "rsquared", "stdErrorBeta")),
          f"beta={reg.get('beta')}")
    check("6mo window ~180 days",
          179 <= (rr["window"]["to"] - rr["window"]["from"]) / 86400 <= 181)
    check("correlation + ratio summarised",
          rr["correlation"]["last"] is not None and rr["ratio"]["last"] is not None)

    print("beta-neutral pair trade")
    from server import pair_trade
    pp = pair_trade.analyze_pair("AMD", "NVDA", capital=10_000)
    check("market beta for both legs",
          all(t in pp["market_beta"] for t in ("AMD", "NVDA")))
    check("signal emitted", isinstance(pp["signal"], str) and pp["signal"])
    if pp["sizing"]:
        net = abs(pp["sizing"]["net_beta_dollars"])
        check("legs are beta-neutral (net ~0)", net < 1.0, f"net=${net}")
        check("share counts positive",
              pp["sizing"]["long"]["shares"] > 0 and pp["sizing"]["short"]["shares"] > 0)

    print("historical prices (Gödel tv-advanced/bars)")
    from server import prices as hp
    pr2 = hp.historical_prices("AMD", resolution="1D", months=6)
    check("bars returned", pr2["count"] > 50, f"{pr2['count']} bars")
    check("bars in window",
          pr2["bars"][0]["time"] >= pr2["window"]["from"] - 5 * 86400)
    check("ohlcv fields present",
          all(k in pr2["bars"][-1] for k in ("open", "high", "low", "close", "volume")))
    pr3 = hp.historical_prices("AMD", count_back=5)
    check("count_back returns N bars", pr3["count"] == 5, f"{pr3['count']}")

    print("safety: no trading")
    import os
    os.environ.pop("GODEL_ALLOW_TRADING", None)
    from broker.orders import OrderManager, TradingDisabledError
    try:
        OrderManager.__new__(OrderManager)  # no network
        blocked = False
        try:
            from broker import orders
            orders._require_trading_enabled()
        except TradingDisabledError:
            blocked = True
    except Exception:
        blocked = True
    check("order placement blocked by default", blocked)

    from agent.tools import run_command
    check("agent allowlist blocks orders",
          "BLOCKED" in run_command("python3 -m broker.cli order BE BUY 1 --confirm"))
    check("agent allowlist allows reads", "BLOCKED" not in run_command("ls output"))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
