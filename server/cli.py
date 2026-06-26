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

from . import api_client, backfill, chat, news, token_store
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
    """Print api endpoints the extension has observed the app calling."""
    path = token_store.STORE_DIR / "endpoints.log"
    if not path.exists():
        print("no endpoints captured yet. Reload the extension and click around "
              "the terminal (open News, RES, etc.).")
        return
    lines = sorted(set(path.read_text().splitlines()))
    print(f"{len(lines)} distinct endpoints observed:\n")
    for ln in lines:
        print("  " + ln)


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

    sub.add_parser("discover").set_defaults(func=cmd_discover)

    r = sub.add_parser("raw")
    r.add_argument("path")
    r.add_argument("--param", action="append", help="key=value (repeatable)")
    r.set_defaults(func=cmd_raw)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
