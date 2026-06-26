"""Order placement / cancellation via the Trader API.

SAFETY: trading is DISABLED by default. Every order method refuses unless the
environment variable GODEL_ALLOW_TRADING=1 is set. Agents run without that
variable, so they physically cannot place or cancel orders even if they invoke
these methods. The CLI additionally requires an explicit --confirm flag.
"""
import os

from .accounts import AccountClient


class TradingDisabledError(RuntimeError):
    pass


def _require_trading_enabled():
    if os.environ.get("GODEL_ALLOW_TRADING") != "1":
        raise TradingDisabledError(
            "Trading is disabled. Set GODEL_ALLOW_TRADING=1 in your own shell to "
            "enable order placement. (Agents and the research toolkit run without "
            "it by design, so they cannot trade.)")


def _equity_leg(symbol: str, instruction: str, quantity: int) -> dict:
    return {
        "instruction": instruction.upper(),
        "quantity": int(quantity),
        "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
    }


class OrderManager:
    def __init__(self, account: AccountClient | None = None):
        self.account = account or AccountClient()

    def _submit(self, payload: dict) -> dict:
        _require_trading_enabled()
        path = f"/accounts/{self.account.account_hash}/orders"
        resp = self.account.post(
            path, json=payload, headers={"Content-Type": "application/json"})
        return resp if isinstance(resp, dict) else {"submitted": True}

    def market_order(self, symbol: str, instruction: str, quantity: int) -> dict:
        return self._submit({
            "orderType": "MARKET", "session": "NORMAL", "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [_equity_leg(symbol, instruction, quantity)],
        })

    def limit_order(self, symbol: str, instruction: str, quantity: int,
                    price: float) -> dict:
        price_str = str(round(price, 4) if price < 1 else round(price, 2))
        return self._submit({
            "orderType": "LIMIT", "price": price_str, "session": "NORMAL",
            "duration": "DAY", "orderStrategyType": "SINGLE",
            "orderLegCollection": [_equity_leg(symbol, instruction, quantity)],
        })

    def cancel(self, order_id: str) -> dict:
        _require_trading_enabled()
        self.account.delete(f"/accounts/{self.account.account_hash}/orders/{order_id}")
        return {"cancelled": order_id}
