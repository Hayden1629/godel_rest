"""Godel REST CLI.

    python -m server.cli status              # token + db summary
    python -m server.cli verify              # Task 1: confirm API answers 200
    python -m server.cli chat [--channel ID] # stream chat -> SQLite + stdout
    python -m server.cli news [--sources ..] # stream news -> SQLite + stdout
    python -m server.cli channels            # discover channelIds from layout
    python -m server.cli raw PATH            # GET any api path, print JSON
"""
import argparse
import json
import time

from . import api_client, backfill, chat, news, pair_trade, prices, ratio, research, token_store
from .sink import Sink


def cmd_status(args):
    rec = token_store.load()
    if not rec:
        print("token: NONE captured. Run token_server + load the extension.")
    else:
        exp = rec.get("exp")
        secs = token_store.seconds_until_expiry()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "?"
        claims = token_store.decode_claims(rec["token"])
        print(f"token: captured, sub={claims.get('sub')}")
        print(f"  expires {when} ({secs} s; {secs/3600:.1f} h)" if secs else f"  expires {when}")
    s = Sink()
    msgs = s.conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    tix = s.conn.execute("SELECT COUNT(*) FROM chat_tickers").fetchone()[0]
    nws = s.conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
    print(f"db: {msgs} chat messages, {tix} ticker mentions, {nws} news items")
    s.close()


def cmd_verify(args):
    """Task 1: prove the token works server-side with a plain request."""
    try:
        page = chat.fetch_page(chat.DEFAULT_CHANNEL, size=5)
    except Exception as e:
        print(f"FAIL: {e}")
        return
    n = len(page.get("content") or [])
    print(f"OK: 200 from chat endpoint, {n} messages, "
          f"totalItems={page.get('totalItems')}")
    print(f"  cursors: next={page.get('nextCursor')!r} prev={page.get('prevCursor')!r}")
    if page.get("content"):
        m = page["content"][0]
        print(f"  newest: [{m.get('createdAt')}] {m.get('username')}: "
              f"{(m.get('content') or '')[:80]}")


def cmd_chat(args):
    names = [c.strip() for c in args.channels.split(",")] if args.channels else None
    if not names:
        chat.poll(channel_id=chat.DEFAULT_CHANNEL, interval=args.interval,
                  size=args.size)
        return
    channels = chat.resolve_channels(names)
    chat.poll_multi(channels, interval=args.interval, size=args.size)


def cmd_backfill(args):
    if args.channels.strip().lower() == "all":
        types = set(args.types.split(",")) if args.types else None
        chans = [
            (c["id"], c.get("title", "?"))
            for c in chat.list_channels()
            if (types is None or c.get("type") in types)
        ]
    else:
        chans = chat.resolve_channels([c.strip() for c in args.channels.split(",")])
    print(f"backfilling {len(chans)} channel(s): "
          f"{', '.join('#'+t for _, t in chans)}")
    backfill.backfill_many(
        chans, page_size=args.size, max_pages=args.max_pages,
        delay=args.delay, resume=not args.no_resume,
    )


def cmd_news(args):
    news.poll_news(interval=args.interval, important=args.important)


def cmd_trending(args):
    rows = news.fetch_trending(timeframe=args.timeframe)
    print(f"trending {args.timeframe} ({len(rows)} tickers, by mentions):")
    for r in rows:
        print(f"  {r['total']:>5}  {r['ticker']:<10} {r['name'] or ''}")


def cmd_channels(args):
    """List all chat channels (id, title, type, last activity)."""
    from collections import defaultdict
    chans = chat.list_channels()
    by = defaultdict(list)
    for c in chans:
        by[c.get("type")].append(c)
    only = set(args.type.split(",")) if args.type else None
    print(f"{len(chans)} channels\n")
    for typ in sorted(by):
        if only and typ not in only:
            continue
        rows = sorted(by[typ], key=lambda c: c.get("lastMessageCreatedAt") or "",
                      reverse=True)
        print(f"=== {typ} ({len(rows)}) ===")
        for c in rows:
            last = (c.get("lastMessageCreatedAt") or "")[:19]
            print(f"  {c.get('title','?'):24} {c['id']}  last={last}")
        print()


def cmd_discover(args):
    """Print api endpoints the extension has observed the app calling, grouped by
    the terminal command that triggered them (from the endpoint monitor).

    The richer capture (command, HTTP status, response sample) lives in
    endpoints.jsonl; falls back to the plain endpoints.log if that's absent."""
    jsonl = token_store.STORE_DIR / "endpoints.jsonl"
    if jsonl.exists():
        rows = []
        for ln in jsonl.read_text().splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        if args.command:
            want = args.command.lower()
            rows = [r for r in rows if want in (r.get("command") or "").lower()]
        if args.status:
            rows = [r for r in rows if str(r.get("status")) == str(args.status)]
        from collections import defaultdict
        by_cmd = defaultdict(list)
        for r in rows:
            by_cmd[r.get("command") or "-"].append(r)
        print(f"{len(rows)} calls across {len(by_cmd)} command context(s):\n")
        for cmd in sorted(by_cmd):
            print(f"=== {cmd} ===")
            for r in by_cmd[cmd]:
                st = r.get("status")
                flag = "" if str(st) == "200" else f"  <- {st}"
                smp = "  [sample]" if r.get("sample_file") else ""
                print(f"  {r.get('method',''):<5} {r.get('path','')}{flag}{smp}")
            print()
        return

    path = token_store.STORE_DIR / "endpoints.log"
    if not path.exists():
        print("no endpoints captured yet. Run `python -m server.token_server`, "
              "reload the extension, and run the command in the terminal "
              "(e.g. type a ticker then TAS / HALT / TRAN).")
        return
    lines = sorted(set(path.read_text().splitlines()))
    print(f"{len(lines)} distinct endpoints observed:\n")
    for ln in lines:
        print("  " + ln)


def cmd_ratio(args):
    """Regress one ticker (y) against another (x) and print every stat:
    beta, alpha, r, r-squared, std errors, t/p values, correlation, ratio."""
    res = ratio.ratio_analysis(
        args.y, args.x, months=args.months,
        correlation_window=args.window,
        from_ts=args.from_ts, to_ts=args.to_ts,
    )
    if args.json:
        out = res if args.raw else {k: v for k, v in res.items() if k != "raw"}
        print(json.dumps(out, indent=2))
        return
    y, x, w = res["y"], res["x"], res["window"]
    reg, corr, rat = res["regression"], res["correlation"], res["ratio"]
    frm = time.strftime("%Y-%m-%d", time.localtime(w["from"]))
    to = time.strftime("%Y-%m-%d", time.localtime(w["to"]))
    print(f"{y['ticker']} (y) regressed against {x['ticker']} (x)")
    print(f"  y: {y['name']} [series {y['series_id']}] last={res['ylast']}")
    print(f"  x: {x['name']} [series {x['series_id']}] last={res['xlast']}")
    print(f"  window: {frm} -> {to} ({w['months']}mo)  corrWindow={corr['window']}")
    print("  regression:")
    for k in ("beta", "adjustedBeta", "alpha", "r", "rsquared", "stdDevError",
              "stdErrorAlpha", "stdErrorBeta", "significance", "ttest",
              "lastTValue", "lastPValue"):
        if k in reg:
            print(f"    {k:14} {reg[k]}")
    print(f"  correlation: last={corr['last']} low={corr['low']} high={corr['high']}")
    print(f"  ratio:       last={rat['last']} low={rat['low']} high={rat['high']}")


def cmd_pair(args):
    """Build a beta-neutral pair trade for two tickers."""
    res = pair_trade.analyze_pair(
        args.a, args.b, benchmark=args.benchmark, capital=args.capital,
        months=args.months, entry_z=args.entry,
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return
    p, rel, r, mb = res["pair"], res["relationship"], res["ratio"], res["market_beta"]
    print(f"PAIR  {p['a']} vs {p['b']}   (market beta vs {p['benchmark']})")
    print(f"  relationship: corr={rel['correlation']:.3f} "
          f"r2={rel['rsquared']:.3f} hedge_beta({p['a']}~{p['b']})={rel['hedge_beta_a_on_b']:.3f}")
    hl = r["half_life_days"]
    print(f"  ratio {p['a']}/{p['b']}: last={r['last']:.4f} mean={r['mean']:.4f} "
          f"z={r['z']:+.2f} pct={r['percentile']*100:.0f}%  "
          f"half-life={hl:.1f}d" if hl else
          f"  ratio {p['a']}/{p['b']}: last={r['last']:.4f} mean={r['mean']:.4f} "
          f"z={r['z']:+.2f} pct={r['percentile']*100:.0f}%  half-life=n/a")
    print(f"  market beta: {p['a']}={mb[p['a']]:.3f}  {p['b']}={mb[p['b']]:.3f}")
    print(f"\n  SIGNAL: {res['signal']}  (entry |z|>{r['entry_z']})")
    s = res["sizing"]
    if s:
        for leg in (s["long"], s["short"]):
            print(f"    {leg['side']:5} {leg['ticker']:6} {leg['shares']:>6} sh "
                  f"@ {leg['price']:.2f} = ${leg['dollars']:>10,.2f}  (beta {leg['beta']:.2f})")
        print(f"    net market-beta exposure: ${s['net_beta_dollars']:,.2f} "
              f"({s['net_beta_pct_of_capital']*100:+.2f}% of ${s['capital']:,.0f} capital)")
    for w in res["warnings"]:
        print(f"  ! {w}")


def cmd_prices(args):
    """Historical OHLCV prices for a ticker. Prints a table, JSON, or writes
    an Excel/CSV file."""
    res = prices.historical_prices(
        args.ticker, resolution=args.resolution, months=args.months,
        from_ts=args.from_ts, to_ts=args.to_ts, count_back=args.count,
    )
    if args.excel:
        prices.to_excel(res, args.excel)
        print(f"wrote {res['count']} bars -> {args.excel}")
        return
    if args.csv:
        prices.to_csv(res, args.csv)
        print(f"wrote {res['count']} bars -> {args.csv}")
        return
    if args.json:
        out = res if args.raw else {k: v for k, v in res.items() if k != "raw"}
        print(json.dumps(out, indent=2))
        return
    print(f"{res['ticker']} {res['name']} [series {res['series_id']}] "
          f"{res['resolution']}  ({res['count']} bars)")
    print(f"{'date':<12}{'open':>12}{'high':>12}{'low':>12}{'close':>12}{'volume':>16}")
    for b in res["bars"]:
        print(f"{b['date']:<12}{b['open']:>12}{b['high']:>12}{b['low']:>12}"
              f"{b['close']:>12}{b['volume']:>16,.0f}")


def cmd_research_list(args):
    rows, total = research.list_page(offset=args.offset, limit=args.limit)
    print(f"page: offset={args.offset} limit={args.limit}  total_items={total:,}"
          f"  total_pages={-(-total // args.limit):,}")
    for r in rows:
        tickers = ",".join(r.get("ticker") or []) or "-"
        print(f"  [{r['id']}] {r.get('date')}  {r.get('provider')}  ({tickers})  "
              f"{(r.get('title') or '')[:80]}")


def cmd_research_fetch(args):
    for rid in args.ids:
        row = research.get_by_id(rid) or {"id": rid, "title": f"id{rid}"}
        path = research.download_one(row)
        print(f"saved {path}  ({row.get('provider', '?')} / {row.get('date', '?')})")


def cmd_research_sync(args):
    n = research.sync_metadata(delay=args.delay)
    c = research.counts()
    print(f"synced {n} rows this run. db totals: {c['total']} total, "
          f"{c['downloaded']} downloaded, {c['pending']} pending, {c['errored']} errored")


def cmd_research_status(args):
    c = research.counts()
    print(f"research.db ({research.DB_PATH}):")
    print(f"  total:      {c['total']:,}")
    print(f"  downloaded: {c['downloaded']:,}")
    print(f"  pending:    {c['pending']:,}")
    print(f"  errored:    {c['errored']:,}")


def cmd_research_backfill(args):
    res = research.download_pending(limit=args.limit, delay=args.delay,
                                     min_free_gb=args.min_free_gb,
                                     workers=args.workers)
    print(f"attempted {res['attempted']} ({res['ok']} ok, {res['failed']} failed)")
    if res["stopped_low_disk"]:
        print(f"stopped early: free disk space fell below {args.min_free_gb}GB. "
              "Re-run once you've freed/added space -- already-downloaded reports "
              "are skipped automatically.")


def cmd_raw(args):
    params = dict(p.split("=", 1) for p in args.param) if args.param else None
    print(json.dumps(api_client.get(args.path, params=params), indent=2))


def main():
    p = argparse.ArgumentParser(prog="godel-rest")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("verify").set_defaults(func=cmd_verify)

    c = sub.add_parser("chat")
    c.add_argument("--channels", default=None,
                   help="comma list of titles or uuids, e.g. general,biotech,options")
    c.add_argument("--interval", type=float, default=3.0)
    c.add_argument("--size", type=int, default=50)
    c.set_defaults(func=cmd_chat)

    b = sub.add_parser("backfill")
    b.add_argument("--channels", required=True,
                   help="comma list of titles/uuids, or 'all'")
    b.add_argument("--types", default="user_write,user_only,public_write,admin_write",
                   help="when channels=all, only these types (comma list)")
    b.add_argument("--size", type=int, default=50)
    b.add_argument("--max-pages", type=int, default=None,
                   help="cap pages per channel (omit for full history)")
    b.add_argument("--delay", type=float, default=0.25,
                   help="seconds between page requests")
    b.add_argument("--no-resume", action="store_true",
                   help="restart from newest instead of oldest stored")
    b.set_defaults(func=cmd_backfill)

    n = sub.add_parser("news")
    n.add_argument("--important", action="store_true",
                   help="only important headlines")
    n.add_argument("--interval", type=float, default=30.0)
    n.set_defaults(func=cmd_news)

    t = sub.add_parser("trending")
    t.add_argument("--timeframe", default="24H")
    t.set_defaults(func=cmd_trending)

    ch = sub.add_parser("channels")
    ch.add_argument("--type", default=None,
                    help="filter by type, e.g. user_write,user_only")
    ch.set_defaults(func=cmd_channels)

    dsc = sub.add_parser("discover",
                         help="endpoints the monitor captured, grouped by command")
    dsc.add_argument("--command", default=None,
                     help="filter to a command context substring, e.g. excel or FA")
    dsc.add_argument("--status", default=None,
                     help="filter by HTTP status, e.g. 404")
    dsc.set_defaults(func=cmd_discover)

    ra = sub.add_parser("ratio",
                        help="regress ticker y against ticker x (beta, r2, std err, ...)")
    ra.add_argument("y", help="dependent ticker, e.g. AMD")
    ra.add_argument("x", help="independent ticker / benchmark, e.g. SOX")
    ra.add_argument("--months", type=float, default=ratio.DEFAULT_MONTHS,
                    help="lookback months (default 6); ignored if --from given")
    ra.add_argument("--window", type=int, default=ratio.DEFAULT_CORRELATION_WINDOW,
                    help="rolling correlation window (default 120)")
    ra.add_argument("--from", dest="from_ts", type=int, default=None,
                    help="explicit window start (unix seconds)")
    ra.add_argument("--to", dest="to_ts", type=int, default=None,
                    help="explicit window end (unix seconds; default now)")
    ra.add_argument("--json", action="store_true", help="emit JSON")
    ra.add_argument("--raw", action="store_true",
                    help="with --json, include the full API payload (timeseries)")
    ra.set_defaults(func=cmd_ratio)

    pa = sub.add_parser("pair",
                        help="beta-neutral pair-trade plan for two tickers")
    pa.add_argument("a", help="first ticker, e.g. PLTR")
    pa.add_argument("b", help="second ticker, e.g. HIMS")
    pa.add_argument("--benchmark", default=pair_trade.DEFAULT_BENCHMARK,
                    help="market proxy for beta (default SPY)")
    pa.add_argument("--capital", type=float, default=pair_trade.DEFAULT_CAPITAL,
                    help="gross capital to deploy across both legs")
    pa.add_argument("--months", type=float, default=ratio.DEFAULT_MONTHS,
                    help="lookback months (default 6)")
    pa.add_argument("--entry", type=float, default=pair_trade.DEFAULT_ENTRY_Z,
                    help="|z| entry threshold (default 1.5)")
    pa.add_argument("--json", action="store_true", help="emit JSON")
    pa.set_defaults(func=cmd_pair)

    hp = sub.add_parser("prices",
                        help="historical OHLCV prices for a ticker (JSON/Excel/CSV)")
    hp.add_argument("ticker", help="ticker, e.g. AMD")
    hp.add_argument("--resolution", default=prices.DEFAULT_RESOLUTION,
                    help="1,5,15,30,60,1D,1W,1M (default 1D)")
    hp.add_argument("--months", type=float, default=prices.DEFAULT_MONTHS,
                    help="lookback months (default 6); ignored if --from or --count given")
    hp.add_argument("--from", dest="from_ts", type=int, default=None,
                    help="window start (unix seconds)")
    hp.add_argument("--to", dest="to_ts", type=int, default=None,
                    help="window end (unix seconds; default now)")
    hp.add_argument("--count", type=int, default=None,
                    help="return exactly N most-recent bars (overrides the window)")
    hp.add_argument("--json", action="store_true", help="emit JSON")
    hp.add_argument("--raw", action="store_true",
                    help="with --json, include the full API payload")
    hp.add_argument("--excel", metavar="PATH", help="write bars to an .xlsx file")
    hp.add_argument("--csv", metavar="PATH", help="write bars to a .csv file")
    hp.set_defaults(func=cmd_prices)

    rl = sub.add_parser("research-list",
                        help="page through RES report metadata (id/title/provider/date), no download")
    rl.add_argument("--offset", type=int, default=0)
    rl.add_argument("--limit", type=int, default=research.PAGE_SIZE)
    rl.set_defaults(func=cmd_research_list)

    rf = sub.add_parser("research-fetch",
                        help="download specific report ids as PDFs (test a couple first)")
    rf.add_argument("ids", nargs="+", type=int)
    rf.set_defaults(func=cmd_research_fetch)

    rs = sub.add_parser("research-sync",
                        help="upsert metadata for every RES report into research.db (no PDFs)")
    rs.add_argument("--delay", type=float, default=0.15)
    rs.set_defaults(func=cmd_research_sync)

    rst = sub.add_parser("research-status", help="research.db totals: downloaded/pending/errored")
    rst.set_defaults(func=cmd_research_status)

    rb = sub.add_parser("research-backfill",
                        help="download every not-yet-downloaded report's PDF (resumable)")
    rb.add_argument("--limit", type=int, default=None, help="cap this run (omit for all pending)")
    rb.add_argument("--workers", type=int, default=8,
                     help="parallel downloads (default 8; back off if you see 429s)")
    rb.add_argument("--delay", type=float, default=0.0,
                     help="extra pause per download, on top of server-driven backoff")
    rb.add_argument("--min-free-gb", type=float, default=5.0, dest="min_free_gb",
                     help="stop once free disk space drops below this many GB")
    rb.set_defaults(func=cmd_research_backfill)

    r = sub.add_parser("raw")
    r.add_argument("path")
    r.add_argument("--param", action="append", help="key=value (repeatable)")
    r.set_defaults(func=cmd_raw)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
