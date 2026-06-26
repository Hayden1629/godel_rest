"""Quotes and market hours via the Schwab Market Data API."""
from datetime import datetime

from . import config
from .base import BaseClient


class MarketDataClient(BaseClient):
    def __init__(self):
        super().__init__(config.MARKETDATA_BASE)

    def quote(self, symbol: str) -> dict | None:
        data = self.get("/quotes", params={"symbols": symbol.upper()}) or {}
        return data.get(symbol.upper())

    def quotes(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        data = self.get("/quotes",
                        params={"symbols": ",".join(s.upper() for s in symbols)})
        return data or {}

    def last_price(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        if not q:
            return None
        qd = q.get("quote", q)
        return qd.get("lastPrice") or qd.get("mark") or qd.get("closePrice")

    def market_hours(self, market: str = "equity", date: str | None = None) -> dict:
        date = date or datetime.utcnow().strftime("%Y-%m-%d")
        return self.get("/markets/hours",
                        params={"markets": market.upper(), "date": date}) or {}

    def is_market_open(self) -> bool:
        data = self.market_hours("equity")
        equity = data.get("equity", {})
        for v in equity.values():
            if isinstance(v, dict) and v.get("isOpen"):
                return True
        return False
