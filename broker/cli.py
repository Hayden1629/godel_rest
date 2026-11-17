"""Schwab broker CLI.

    python3 -m broker.cli auth                  # token status
    python3 -m broker.cli balances              # cash, equity, buying power
    python3 -m broker.cli positions             # open positions
    python3 -m broker.cli portfolio             # holdings + exposure
    python3 -m broker.cli risk                  # beta/vol/concentration (uses Massive)
    python3 -m broker.cli quote AAPL            # live quote
    python3 -m broker.cli hours                 # is the market open
    python3 -m broker.cli propose AAPL BUY 10                   # ask Hayden
    python3 -m broker.cli propose AAPL BUY 10 --limit 190
    python3 -m broker.cli propose-cancel <ORDER_ID>
    python3 -m broker.cli confirm t-1a2b3c4d --yes              # place it
    python3 -m broker.cli pending                               # live requests
    python3 -m broker.cli revoke [TICKET]                       # kill request(s)
    python3 -m broker.cli order AAPL BUY 10 --confirm           # MANUAL only

Trading model: `propose` size-checks the order and returns the exact message to
put in front of Hayden. He replies YES in the normal chat; `confirm --yes`
places precisely the order he was shown. One approval, one order, 15-minute
expiry. Set GODEL_CONFIRM_MODE=code for the stricter typed-code flow. `order`
is the direct human path, needs GODEL_ALLOW_TRADING=1 in your own shell, and is
not on the agent allowlist. Panic: GODEL_TRADING_HALT=1 refuses everything.

First-time setup:  python3 -m broker.setup_auth
"""
import argparse
import json
import sys


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_auth(_):
    from .tokens import get_token_manager
    _print(get_token_manager().status())


def cmd_balances(_):
    from .accounts import AccountClient
    _print(AccountClient().balances())


def cmd_positions(_):
    from .accounts import AccountClient
    _print(AccountClient().positions())


def cmd_portfolio(_):
    from .portfolio import Portfolio
    _print(Portfolio().snapshot())


def cmd_risk(args):
    from .portfolio import Portfolio
    from analytics import portfolio_risk
    snap = Portfolio().snapshot()
    result = portfolio_risk.analyze(snap, lookback_days=args.lookback)
    if args.json:
        _print(result)
    else:
        print(portfolio_risk.format_report(result))


def cmd_fit(args):
    from .portfolio import Portfolio
    from analytics import portfolio_fit
    snap = Portfolio().snapshot()
    holdings = [{"symbol": p["symbol"], "market_value": p["market_value"]}
                for p in snap["positions"] if p.get("asset_type") == "EQUITY"]
    equity = snap["balances"].get("liquidation_value")
    _print(portfolio_fit.analyze_fit(args.ticker, holdings, equity=equity,
                                     lookback_days=args.lookback))


def cmd_quote(args):
    from .market_data import MarketDataClient
    _print(MarketDataClient().quote(args.symbol))


def cmd_hours(_):
    from .market_data import MarketDataClient
    print("market open:", MarketDataClient().is_market_open())


def cmd_propose(args):
    from . import authorization
    res = authorization.propose(args.symbol, args.side, args.quantity,
                                limit=args.limit, note=args.note or "",
                                duration=args.duration)
    _print(res)
    if res.get("send_to_hayden"):
        print("\n--- send this to Hayden verbatim, then wait for YES ---\n"
              + res["send_to_hayden"], file=sys.stderr)
    sz = res["sizing"]
    if sz["breach"]:
        print("\n*** SIZE WARNING: " + sz["detail"], file=sys.stderr)
        print("*** Above your sizing guidance. Hayden's confirmation approves "
              "the oversize position knowingly.", file=sys.stderr)
    elif sz["ok"] is None:
        print("\n*** SIZE UNVERIFIED: " + sz["detail"], file=sys.stderr)


def cmd_propose_cancel(args):
    from . import authorization
    _print(authorization.propose_cancel(args.order_id, note=args.note or ""))


def cmd_confirm(args):
    from . import authorization
    from .orders import OrderManager
    if not args.code and not args.yes:
        print("Refusing: pass --yes (Hayden approved in chat) or the code.",
              file=sys.stderr)
        sys.exit(2)
    try:
        _print(OrderManager().place_authorized(args.ticket, args.code))
    except (authorization.AuthorizationError, authorization.TradingHalted) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(3)


def cmd_pending(_):
    from . import authorization
    _print(authorization.pending())


def cmd_revoke(args):
    from . import authorization
    n = authorization.revoke(args.ticket or "")
    print(f"revoked {n} authorization request(s)")


def cmd_order(args):
    if not args.confirm:
        print("Refusing to place a real order without --confirm.", file=sys.stderr)
        sys.exit(2)
    from .orders import OrderManager
    om = OrderManager()
    if args.limit is not None:
        _print(om.limit_order(args.symbol, args.side, args.quantity, args.limit))
    else:
        _print(om.market_order(args.symbol, args.side, args.quantity))


def main():
    p = argparse.ArgumentParser(prog="broker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth").set_defaults(func=cmd_auth)
    sub.add_parser("balances").set_defaults(func=cmd_balances)
    sub.add_parser("positions").set_defaults(func=cmd_positions)
    sub.add_parser("portfolio").set_defaults(func=cmd_portfolio)
    r = sub.add_parser("risk")
    r.add_argument("--lookback", type=int, default=365)
    r.add_argument("--json", action="store_true",
                   help="machine-readable JSON (for agents); default is human report")
    r.set_defaults(func=cmd_risk)
    fit = sub.add_parser("fit")
    fit.add_argument("ticker")
    fit.add_argument("--lookback", type=int, default=365)
    fit.set_defaults(func=cmd_fit)

    q = sub.add_parser("quote")
    q.add_argument("symbol")
    q.set_defaults(func=cmd_quote)
    sub.add_parser("hours").set_defaults(func=cmd_hours)
    pr = sub.add_parser("propose", help="request Hayden's authorization for a trade")
    pr.add_argument("symbol")
    pr.add_argument("side", choices=["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"])
    pr.add_argument("quantity", type=int)
    pr.add_argument("--limit", type=float, default=None)
    pr.add_argument("--duration", default="DAY", choices=["DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL", "IMMEDIATE_OR_CANCEL"],
                    help="order duration (default: DAY)")
    pr.add_argument("--note", default="", help="one line of context for Hayden")
    pr.set_defaults(func=cmd_propose)

    pc = sub.add_parser("propose-cancel", help="request authorization to cancel")
    pc.add_argument("order_id")
    pc.add_argument("--note", default="")
    pc.set_defaults(func=cmd_propose_cancel)

    cf = sub.add_parser("confirm", help="place a trade Hayden authorized")
    cf.add_argument("ticket")
    cf.add_argument("code", nargs="?", default=None,
                    help="only in GODEL_CONFIRM_MODE=code")
    cf.add_argument("--yes", action="store_true",
                    help="Hayden approved this proposal in chat")
    cf.set_defaults(func=cmd_confirm)

    sub.add_parser("pending", help="live authorization requests").set_defaults(
        func=cmd_pending)
    rv = sub.add_parser("revoke", help="kill pending request(s)")
    rv.add_argument("ticket", nargs="?", default="")
    rv.set_defaults(func=cmd_revoke)

    o = sub.add_parser("order")
    o.add_argument("symbol")
    o.add_argument("side", choices=["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"])
    o.add_argument("quantity", type=int)
    o.add_argument("--limit", type=float, default=None)
    o.add_argument("--confirm", action="store_true")
    o.set_defaults(func=cmd_order)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
