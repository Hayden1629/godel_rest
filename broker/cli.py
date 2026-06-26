"""Schwab broker CLI.

    python3 -m broker.cli auth                  # token status
    python3 -m broker.cli balances              # cash, equity, buying power
    python3 -m broker.cli positions             # open positions
    python3 -m broker.cli portfolio             # holdings + exposure
    python3 -m broker.cli risk                  # beta/vol/concentration (uses Massive)
    python3 -m broker.cli quote AAPL            # live quote
    python3 -m broker.cli hours                 # is the market open
    python3 -m broker.cli order AAPL BUY 10 --confirm          # market order
    python3 -m broker.cli order AAPL BUY 10 --limit 190 --confirm

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
