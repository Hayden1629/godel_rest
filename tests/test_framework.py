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
