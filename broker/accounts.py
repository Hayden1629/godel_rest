"""Account data: balances, positions, orders (read). Uses the Trader API."""
from datetime import datetime, timedelta

from . import config
from .base import BaseClient

OPEN_ORDER_STATUSES = {"WORKING", "PENDING_ACTIVATION", "QUEUED", "ACCEPTED",
                       "AWAITING_PARENT_ORDER", "PENDING"}


class AccountClient(BaseClient):
    def __init__(self):
        super().__init__(config.TRADER_BASE)
        self._account_hash = None

    @property
    def account_hash(self) -> str:
        """The encrypted account id Schwab requires in account/order URLs."""
        if self._account_hash is None:
            data = self.get("/accounts/accountNumbers") or []
            if not data:
                raise RuntimeError("No Schwab accounts returned for this login.")
            self._account_hash = data[0]["hashValue"]
        return self._account_hash

    def account(self, with_positions: bool = True) -> dict:
        """Raw account payload (balances + optionally positions)."""
        params = {"fields": "positions"} if with_positions else None
        data = self.get(f"/accounts/{self.account_hash}", params=params) or {}
        return data.get("securitiesAccount", {})

    def balances(self) -> dict:
        """Key balance figures, flattened."""
        acct = self.account(with_positions=False)
        cur = acct.get("currentBalances", {})
        return {
            "account_id": acct.get("accountNumber"),
            "type": acct.get("type"),
            "liquidation_value": cur.get("liquidationValue"),
            "cash": cur.get("cashBalance"),
            "buying_power": cur.get("buyingPower"),
            "equity": cur.get("equity"),
            "long_market_value": cur.get("longMarketValue"),
            "short_market_value": cur.get("shortMarketValue"),
        }

    def positions(self) -> list[dict]:
        """Normalized open positions (zero-quantity filtered out)."""
        acct = self.account(with_positions=True)
        out = []
        for p in acct.get("positions", []):
            long_q = p.get("longQuantity", 0) or 0
            short_q = p.get("shortQuantity", 0) or 0
            if long_q == 0 and short_q == 0:
                continue
            instrument = p.get("instrument", {})
            qty = long_q - short_q
            out.append({
                "symbol": instrument.get("symbol"),
                "asset_type": instrument.get("assetType"),
                "quantity": qty,
                "side": "LONG" if qty > 0 else "SHORT",
                "market_value": p.get("marketValue"),
                "average_price": p.get("averagePrice"),
                "day_pl": p.get("currentDayProfitLoss"),
                "day_pl_pct": p.get("currentDayProfitLossPercentage"),
                "long_open_pl": p.get("longOpenProfitLoss"),
            })
        return out

    def open_orders(self) -> list[dict]:
        eastern_now = datetime.utcnow()
        params = {
            "fromEnteredTime": (eastern_now - timedelta(days=1)).isoformat() + "Z",
            "toEnteredTime": eastern_now.isoformat() + "Z",
        }
        orders = self.get(f"/accounts/{self.account_hash}/orders", params=params) or []
        return [o for o in orders
                if o.get("status", "").upper() in OPEN_ORDER_STATUSES]
