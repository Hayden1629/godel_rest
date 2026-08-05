"""Run Gödel Terminal commands from the shell.

Mirrors the terminal's own mnemonics, so muscle memory carries over. Both word
orders work — command-first or the terminal-native ``TICKER EQ CMD``:

    python -m server.terminal DES BBY
    python -m server.terminal BBY EQ FA          # == FA BBY
    python -m server.terminal FA BSX --statement balance_sheet
    python -m server.terminal EM BBY
    python -m server.terminal ANR BBY
    python -m server.terminal SI BBY
    python -m server.terminal ERN BBY
    python -m server.terminal TREND
    python -m server.terminal MOST --tab GAINERS
    python -m server.terminal IPO

Add ``--json`` for machine-readable output, ``--raw`` for the untouched payload.
"""
import argparse
import json

from . import commands as C

# commands that take a ticker vs. the market-wide ones
_TICKER_CMDS = {"DES", "FA", "EM", "ERN", "SI", "ANR", "TAS", "TRAN"}
_MARKET_CMDS = {"TREND", "MOST", "IPO", "HALT"}
_ALIASES = {"FIN": "FA", "FINANCIALS": "FA", "DESC": "DES", "SHORT": "SI",
            "EARNINGS": "EM", "ESTIMATES": "ERN", "RATINGS": "ANR"}


def _fmt_pct(x, nd=2):
    return "n/a" if x is None else f"{x * 100:.{nd}f}%"


def _fmt_num(x):
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e9:
        return f"{x / 1e9:,.2f}B"
    if ax >= 1e6:
        return f"{x / 1e6:,.2f}M"
    return f"{x:,.0f}"


# ── per-command pretty printers ───────────────────────────────────────────────


def _print_des(d):
    print(f"{d['ticker']} — {d['name']}  [series {d['series_id']}]")
    print(f"  {d.get('sector') or '?'} / {d.get('industry') or '?'}  "
          f"· {d.get('employees') or '?'} employees · CEO {d.get('ceo') or '?'}")
    print(f"  mkt cap {_fmt_num(d.get('marketCapStatic'))}  "
          f"EV {_fmt_num(d.get('enterpriseValue'))}  "
          f"beta {d.get('beta') or 'n/a'}")
    print(f"  P/E {d.get('trailingPe') or 'n/a'} (fwd {d.get('forwardPe') or 'n/a'})  "
          f"P/S {d.get('priceToSalesTtm') or 'n/a'}  P/B {d.get('priceToBookMrq') or 'n/a'}")
    dy = d.get("forwardAnnualDividendYield")
    print(f"  div yield {_fmt_pct(dy) if dy is not None else 'n/a'}  "
          f"payout {_fmt_pct(d.get('payoutRatio')) if d.get('payoutRatio') is not None else 'n/a'}  "
          f"inst held {_fmt_pct(d.get('percentHeldByInstitutions')) if d.get('percentHeldByInstitutions') is not None else 'n/a'}")
    if d.get("description"):
        print(f"\n  {d['description'][:600]}")


def _print_em(d):
    print(f"{d['meta'].get('symbol')} earnings surprises (newest first)")
    print(f"  {'date':<12}{'est':>8}{'actual':>9}{'surprise':>10}")
    for r in d["earnings"]:
        sp = r.get("surprise_prc")
        print(f"  {r.get('date',''):<12}{str(r.get('eps_estimate','')):>8}"
              f"{str(r.get('eps_actual','')):>9}"
              f"{(f'{sp:+.1f}%' if sp is not None else ''):>10}")


def _print_ern(d):
    print(f"{d['meta'].get('symbol')} forward EPS estimates")
    print(f"  {'date':<12}{'period':<18}{'#':>4}{'avg':>9}{'low':>9}{'high':>9}{'yr-ago':>9}")
    for r in d["estimates"]:
        print(f"  {r.get('date',''):<12}{(r.get('period') or ''):<18}"
              f"{str(r.get('number_of_analysts','')):>4}"
              f"{str(r.get('avg_estimate','')):>9}{str(r.get('low_estimate','')):>9}"
              f"{str(r.get('high_estimate','')):>9}{str(r.get('year_ago_eps','')):>9}")


def _print_si(d):
    print(f"short interest [series {d['series_id']}]")
    print(f"  shares short      {_fmt_num(d.get('shortInterest'))}  "
          f"(chg {_fmt_num(d.get('shortInterestChange'))})")
    print(f"  days to cover     {d.get('shortInterestRatio')}  "
          f"(chg {d.get('shortInterestRatioChange')})")
    print(f"  avg daily volume  {_fmt_num(d.get('averageDailyVolume'))}")
    print(f"  range             {_fmt_num(d.get('shortInterestLow'))} .. "
          f"{_fmt_num(d.get('shortInterestHigh'))}")


def _print_anr(d):
    print(f"{d['meta'].get('symbol')} analyst ratings (newest first)")
    print(f"  {'date':<12}{'firm':<26}{'action':<14}{'current':<14}{'prior'}")
    for r in d["ratings"]:
        print(f"  {r.get('date',''):<12}{(r.get('firm') or '')[:25]:<26}"
              f"{(r.get('rating_change') or '')[:13]:<14}"
              f"{(r.get('rating_current') or '')[:13]:<14}{r.get('rating_prior') or ''}")


def _print_trend(d):
    print(f"trending {d['timeframe']} (by mentions)")
    for r in d["trending"]:
        print(f"  {r['total']:>6}  {r['ticker']:<10}{r.get('name') or ''}")


def _print_most(d):
    print(f"most {d['tab'].lower()}")
    print(f"  {'ticker':<10}{'daily $ vol':>16}{'mkt cap':>14}  name")
    for s in d["securities"]:
        print(f"  {(s['ticker'] or '?'):<10}{_fmt_num(s.get('daily_value')):>16}"
              f"{_fmt_num(s.get('market_cap')):>14}  {(s.get('name') or '')[:40]}")


def _print_ipo(d):
    print("IPOs (upcoming & recent)")
    print(f"  {'ticker':<8}{'date':<12}{'status':<10}{'exchange':<10}price")
    for r in d["ipos"]:
        lo, hi = r.get("sharePriceLowest"), r.get("sharePriceHighest")
        price = (f"${r['sharePrice']}" if r.get("sharePrice")
                 else (f"${lo}-{hi}" if lo or hi else "?"))
        print(f"  {(r.get('ticker') or '?'):<8}{(r.get('date') or ''):<12}"
              f"{(r.get('status') or ''):<10}{(r.get('exchange') or ''):<10}{price}")


def _print_fa(d):
    print(f"{d['ticker']} {d['statement']} ({d['period']}) — last {len(d['periods'])}")
    hdr = "  " + "line".ljust(26) + "".join(p[2:].rjust(11) for p in d["periods"])
    print(hdr)
    for name, vals in d["lines"].items():
        cells = "".join(("" if v is None else f"{v:,.0f}"
                         if abs(v) >= 100 else f"{v:.3f}").rjust(11) for v in vals)
        print("  " + name.ljust(26) + cells)


_PRINTERS = {"DES": _print_des, "EM": _print_em, "ERN": _print_ern,
             "SI": _print_si, "ANR": _print_anr, "TREND": _print_trend,
             "MOST": _print_most, "IPO": _print_ipo, "FA": _print_fa}


# ── dispatch ──────────────────────────────────────────────────────────────────


def _normalize_argv(tokens: list[str]) -> tuple[str, str | None]:
    """Accept ``CMD [TICKER]`` or ``TICKER [EQ] CMD``; return (cmd, ticker)."""
    words = [t.upper() for t in tokens]
    words = [_ALIASES.get(w, w) for w in words]
    known = _TICKER_CMDS | _MARKET_CMDS
    cmd = next((w for w in words if w in known), None)
    if cmd is None:
        raise SystemExit(f"unknown command in {tokens!r}. "
                         f"Known: {', '.join(sorted(known))}")
    ticker = next((w for w in words if w not in known and w != "EQ"), None)
    return cmd, ticker


def _run(cmd: str, ticker: str | None, args) -> dict:
    if cmd in _TICKER_CMDS and not ticker:
        raise SystemExit(f"{cmd} needs a ticker, e.g. `{cmd} BBY`")
    if cmd == "DES":
        return C.des(ticker)
    if cmd == "FA":
        return C.fa(ticker, statement=args.statement, period=args.period,
                    quarters=args.quarters)
    if cmd == "EM":
        return C.em(ticker, limit=args.limit)
    if cmd == "ERN":
        return C.ern(ticker)
    if cmd == "SI":
        return C.si(ticker)
    if cmd == "ANR":
        return C.anr(ticker, limit=args.limit)
    if cmd == "TREND":
        return C.trend(timeframe=args.timeframe)
    if cmd == "MOST":
        return C.most(tab=args.tab, rows=args.limit)
    if cmd == "IPO":
        return C.ipo(limit=args.limit)
    if cmd == "TAS":
        return C.tas(ticker)
    if cmd == "TRAN":
        return C.tran(ticker)
    if cmd == "HALT":
        return C.halt()
    raise SystemExit(f"command {cmd} not wired")


def main():
    p = argparse.ArgumentParser(
        prog="godel-terminal",
        description="Run Gödel Terminal commands (DES/FA/EM/ERN/SI/ANR/TREND/"
                    "MOST/IPO). Word order is flexible: `DES BBY` or `BBY EQ DES`.")
    p.add_argument("tokens", nargs="+", help="e.g. DES BBY  |  BBY EQ FA")
    p.add_argument("--statement", default="income_statement",
                   choices=["income_statement", "balance_sheet", "cash_flow"],
                   help="FA only")
    p.add_argument("--period", default="QTR", choices=["QTR", "ANN"],
                   help="FA only")
    p.add_argument("--quarters", type=int, default=8, help="FA columns to show")
    p.add_argument("--limit", type=int, default=20,
                   help="row cap for EM/ANR/MOST/IPO")
    p.add_argument("--tab", default="ACTIVE",
                   choices=["ACTIVE", "GAINERS", "LOSERS"], help="MOST only")
    p.add_argument("--timeframe", default="24H", help="TREND only")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--raw", action="store_true",
                   help="with --json, keep full payloads (e.g. SI time series)")
    args = p.parse_args()

    cmd, ticker = _normalize_argv(args.tokens)
    result = _run(cmd, ticker, args)

    if not result.get("discovered", True):  # NotDiscovered marker
        print(f"{result['command']}: {result['hint']}")
        return
    if args.json:
        out = result
        if not args.raw and isinstance(result, dict):
            out = {k: v for k, v in result.items() if k != "raw"}
        print(json.dumps(out, indent=2, default=str))
        return
    _PRINTERS[cmd](result)


if __name__ == "__main__":
    main()
