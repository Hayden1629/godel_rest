"""Order placement / cancellation via the Trader API.

SAFETY MODEL (changed 2026-08-13 — Hayden can now authorize live orders):

Every order must consume a single-use authorization grant bound to its exact
parameters (see authorization.py). The agent can PROPOSE a trade; only Hayden can complete one, by approving that
specific proposal. There is no standing permission, no "trading enabled" mode,
and no way to reuse a grant for a second order. Grants expire in 15 minutes.

Default confirmation is a plain "yes" in the Telegram chat (GODEL_CONFIRM_MODE
=reply). The order that goes out is locked to the one he was shown.

Two independent things must be true for an order to reach Schwab:
  1. a live grant whose fingerprint matches this exact order, and
  2. GODEL_TRADING_HALT is not set.

The old GODEL_ALLOW_TRADING env var no longer gates the agent's path. It now
gates only the direct human `broker.cli order` command, which exists for
Hayden's own shell and stays off the agent's allowlist.
"""
import os

from . import authorization
from .accounts import AccountClient


class TradingDisabledError(RuntimeError):
    pass


# Re-exported so callers can catch one family of refusals.
AuthorizationError = authorization.AuthorizationError
TradingHalted = authorization.TradingHalted


def _require_manual_trading_enabled():
    """Gate for the direct human order path only (broker.cli order --confirm)."""
    if os.environ.get("GODEL_ALLOW_TRADING") != "1":
        raise TradingDisabledError(
            "Direct order placement is disabled. Set GODEL_ALLOW_TRADING=1 in your "
            "own shell to use `broker.cli order` by hand. Agents do not use this "
            "path — they use propose/confirm, which requires your confirmation "
            "code instead.")


def _equity_leg(symbol: str, instruction: str, quantity: int) -> dict:
    return {
        "instruction": instruction.upper(),
        "quantity": int(quantity),
        "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
    }


def _payload(symbol: str, instruction: str, quantity: int,
             limit: float | None = None, duration: str = "DAY") -> dict:
    base = {
        "session": "NORMAL", "duration": duration, "orderStrategyType": "SINGLE",
        "orderLegCollection": [_equity_leg(symbol, instruction, quantity)],
    }
    if limit is None:
        return {"orderType": "MARKET", **base}
    price = round(limit, 4) if limit < 1 else round(limit, 2)
    return {"orderType": "LIMIT", "price": str(price), **base}


class OrderManager:
    def __init__(self, account: AccountClient | None = None):
        self.account = account or AccountClient()

    # ------------------------------------------------------------ transport
    def _send(self, payload: dict) -> dict:
        path = f"/accounts/{self.account.account_hash}/orders"
        resp = self.account.post(
            path, json=payload, headers={"Content-Type": "application/json"})
        return resp if isinstance(resp, dict) else {"submitted": True}

    # ------------------------------------------- authorized path (the agent)
    def place_authorized(self, ticket_id: str, code: str | None = None) -> dict:
        """The only order path available to the agent.

        Consumes the grant first: a wrong or expired code never reaches the
        broker, and a consumed grant cannot place a second order.
        """
        authorization.check_halt()
        grant = authorization.consume(ticket_id, code)

        if grant.get("action") == "cancel":
            order_id = grant["order_id"]
            self.account.delete(
                f"/accounts/{self.account.account_hash}/orders/{order_id}")
            authorization._audit("cancelled", {"ticket": ticket_id,
                                               "order_id": order_id})
            return {"cancelled": order_id, "ticket_id": ticket_id}

        payload = _payload(grant["symbol"], grant["side"], grant["quantity"],
                           grant["limit"], grant.get("duration", "DAY"))
        # Belt and braces: the payload we are about to send must fingerprint to
        # the grant we just burned. Guards against any drift between the two.
        fp = authorization.order_fingerprint(
            grant["symbol"], grant["side"], grant["quantity"],
            grant["order_type"], grant["limit"])
        if fp != grant["fingerprint"]:
            raise AuthorizationError(
                "Order does not match its authorization. Refusing to send.")

        result = self._send(payload)
        authorization._audit("placed", {
            "ticket": ticket_id, "symbol": grant["symbol"], "side": grant["side"],
            "quantity": grant["quantity"], "order_type": grant["order_type"],
            "limit": grant["limit"], "oversize_approved": grant["sizing_breach"],
            "broker_response": str(result)[:500]})
        return {"placed": True, "ticket_id": ticket_id,
                "order": {k: grant[k] for k in
                          ("symbol", "side", "quantity", "order_type", "limit")},
                "oversize_approved": grant["sizing_breach"],
                "broker_response": result}

    # -------------------------------------------- manual path (Hayden's shell)
    def market_order(self, symbol: str, instruction: str, quantity: int) -> dict:
        authorization.check_halt()
        _require_manual_trading_enabled()
        result = self._send(_payload(symbol, instruction, quantity))
        authorization._audit("placed_manual", {
            "symbol": symbol.upper(), "side": instruction.upper(),
            "quantity": quantity, "order_type": "MARKET"})
        return result

    def limit_order(self, symbol: str, instruction: str, quantity: int,
                    price: float) -> dict:
        authorization.check_halt()
        _require_manual_trading_enabled()
        result = self._send(_payload(symbol, instruction, quantity, price))
        authorization._audit("placed_manual", {
            "symbol": symbol.upper(), "side": instruction.upper(),
            "quantity": quantity, "order_type": "LIMIT", "limit": price})
        return result

    def cancel(self, order_id: str) -> dict:
        """Manual cancel. The agent's route is propose_cancel -> confirm."""
        authorization.check_halt()
        _require_manual_trading_enabled()
        self.account.delete(f"/accounts/{self.account.account_hash}/orders/{order_id}")
        authorization._audit("cancelled", {"order_id": order_id})
        return {"cancelled": order_id}
