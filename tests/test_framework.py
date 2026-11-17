"""Deterministic validation of the research framework (no LLM required).

    python3 -m tests.test_framework

Exits non-zero if any check fails. The agent end-to-end test (LLM-driven) lives
separately in agent/ and is exercised via `python3 -m agent.run "<task>"`.
"""
import json
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

    print("safety: trading requires Hayden's approval")
    import os
    import tempfile
    os.environ.pop("GODEL_ALLOW_TRADING", None)
    os.environ.pop("GODEL_TRADING_HALT", None)
    os.environ["GODEL_PRIVATE_DIR"] = tempfile.mkdtemp(prefix="godel-auth-test-")
    import importlib
    from broker import authorization as auth
    from broker.orders import TradingDisabledError

    # ---- default (reply) mode: Hayden says yes in chat ----
    os.environ["GODEL_CONFIRM_MODE"] = "reply"
    importlib.reload(auth)

    blocked = False
    try:
        from broker import orders
        orders._require_manual_trading_enabled()
    except TradingDisabledError:
        blocked = True
    check("manual order path blocked without GODEL_ALLOW_TRADING", blocked)

    req = auth.propose("AAPL", "BUY", 3, limit=100.0)
    check("reply mode mints no code", "code_delivered_to" not in req)
    check("proposal carries the exact words for Hayden",
          "BUY 3 AAPL" in req.get("send_to_hayden", ""))
    check("proposal always states a size position",
          "size" in req.get("send_to_hayden", "").lower())

    grant = auth.consume(req["ticket_id"])
    check("approval grants exactly what was shown",
          grant["symbol"] == "AAPL" and grant["quantity"] == 3
          and grant["limit"] == 100.0 and grant["order_type"] == "LIMIT")
    replayed = False
    try:
        auth.consume(req["ticket_id"])
    except auth.AuthorizationError:
        replayed = True
    check("approval cannot be replayed", replayed)

    unknown = False
    try:
        auth.consume("t-deadbeef")
    except auth.AuthorizationError:
        unknown = True
    check("unknown ticket refused", unknown)

    req2 = auth.propose("AAPL", "BUY", 1, limit=100.0)
    st = auth._load()
    st["tickets"][req2["ticket_id"]]["expires_at"] = auth._now() - 1
    auth._save(st)
    expired = False
    try:
        auth.consume(req2["ticket_id"])
    except auth.AuthorizationError:
        expired = True
    check("expired approval refused", expired)

    os.environ["GODEL_TRADING_HALT"] = "1"
    halted = False
    try:
        auth.propose("AAPL", "BUY", 1, limit=100.0)
    except auth.TradingHalted:
        halted = True
    check("GODEL_TRADING_HALT stops everything", halted)
    os.environ.pop("GODEL_TRADING_HALT")

    check("grant fingerprint is parameter-bound",
          auth.order_fingerprint("AAPL", "BUY", 1, "LIMIT", 100.0)
          != auth.order_fingerprint("AAPL", "BUY", 2, "LIMIT", 100.0))

    sz = auth.size_check("AAPL", "BUY", 10, limit=1e9)
    check("oversize warns (breach flagged, not raised)",
          sz["breach"] is True or sz["ok"] is None)
    big = auth.propose("AAPL", "BUY", 10, limit=1e9)
    check("oversize proposal says so in Hayden's message",
          "SIZE WARNING" in big.get("send_to_hayden", "")
          or "UNVERIFIED" in big.get("send_to_hayden", ""))

    # ---- strict (code) mode still works for anyone who turns it on ----
    os.environ["GODEL_CONFIRM_MODE"] = "code"
    importlib.reload(auth)
    creq = auth.propose("AAPL", "BUY", 1, limit=100.0)
    conf_log = [c for c in creq["code_delivered_to"] if c.startswith("file:")][0][5:]
    code = [l.split(": ", 1)[1] for l in open(conf_log).read().splitlines()
            if l.startswith("Confirmation code:")][-1]
    check("code mode: code absent from output", code not in json.dumps(creq, default=str))
    store_path = os.path.join(os.environ["GODEL_PRIVATE_DIR"], "trade-tickets.json")
    check("code mode: code absent from ticket store", code not in open(store_path).read())
    refused = False
    try:
        auth.consume(creq["ticket_id"], "ZZZZ-9999")
    except auth.AuthorizationError:
        refused = True
    check("code mode: wrong code refused", refused)
    check("code mode: right code works", auth.consume(creq["ticket_id"], code)["quantity"] == 1)
    os.environ["GODEL_CONFIRM_MODE"] = "reply"
    importlib.reload(auth)

    from agent.tools import run_command
    check("agent allowlist blocks the manual order path",
          "BLOCKED" in run_command("python3 -m broker.cli order BE BUY 1 --confirm"))
    check("agent allowlist blocks reads outside the project",
          "BLOCKED" in run_command("cat ~/.openclaw/trade-tickets.json"))
    check("agent allowlist allows confirm --yes",
          "BLOCKED" not in run_command("python3 -m broker.cli confirm t-1a2b3c4d --yes"))
    check("agent allowlist allows propose",
          "BLOCKED" not in run_command("python3 -m broker.cli pending"))
    check("agent allowlist allows reads", "BLOCKED" not in run_command("ls output"))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
