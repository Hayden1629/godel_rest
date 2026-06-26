"""High-level portfolio view built on the account client: holdings with
weights, plus gross/net exposure."""
from .accounts import AccountClient


class Portfolio:
    def __init__(self, account: AccountClient | None = None):
        self.account = account or AccountClient()

    def snapshot(self) -> dict:
        balances = self.account.balances()
        positions = self.account.positions()
        gross = sum(abs(p["market_value"] or 0) for p in positions)
        net = sum(p["market_value"] or 0 for p in positions)
        longs = sum(p["market_value"] or 0 for p in positions if p["quantity"] > 0)
        shorts = sum(p["market_value"] or 0 for p in positions if p["quantity"] < 0)
        for p in positions:
            mv = p["market_value"] or 0
            p["weight"] = (mv / gross) if gross else 0.0
        positions.sort(key=lambda p: abs(p["market_value"] or 0), reverse=True)
        return {
            "balances": balances,
            "positions": positions,
            "exposure": {
                "gross": gross, "net": net,
                "long": longs, "short": shorts,
                "n_positions": len(positions),
                "cash": balances.get("cash"),
                "net_pct_of_equity": (net / balances["liquidation_value"])
                if balances.get("liquidation_value") else None,
            },
        }

    def symbols(self) -> list[str]:
        return [p["symbol"] for p in self.account.positions()
                if p["asset_type"] == "EQUITY"]
