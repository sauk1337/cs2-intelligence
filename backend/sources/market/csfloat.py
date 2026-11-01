"""CSFloat Market API (key required: https://csfloat.com/profile → Developers → API key).

GET /api/v1/listings?market_hash_name=…&sort_by=lowest_price&type=buy_now&limit=N
Header: `Authorization: <API_KEY>`. Prices in USD cents.
Without a key the API returns 403 "You need to be logged in to search listings".
"""
from __future__ import annotations

from .base import MarketAdapter, Quote

API = "https://csfloat.com/api/v1"


class CSFloat(MarketAdapter):
    name = "csfloat"
    supports_item = True

    @property
    def configured(self) -> bool:
        return bool(self.conf.get("api_key"))

    async def fetch_item(self, market_hash_name: str, phase: str | None = None) -> Quote | None:
        data = await self.http.get_json(self.name, f"{API}/listings", params={
            "market_hash_name": market_hash_name, "sort_by": "lowest_price", "type": "buy_now",
            "limit": int(self.conf.get("listings_limit", 10))},
            headers={"Authorization": str(self.conf.get("api_key"))})
        return self.parse_listings(market_hash_name, data, phase)

    @staticmethod
    def parse_listings(name: str, data, phase: str | None = None) -> Quote | None:
        rows = data.get("data", []) if isinstance(data, dict) else (data or [])
        prices = []
        first_id = None
        for r in rows:
            item = r.get("item") or {}
            if item.get("market_hash_name") and item["market_hash_name"] != name:
                continue
            if phase and (item.get("phase") or "").lower() != phase.lower():
                continue
            if isinstance(r.get("price"), (int, float)):
                prices.append(r["price"] / 100)
                first_id = first_id or r.get("id")
        if not prices:
            return None
        prices.sort()
        median = prices[len(prices) // 2]
        return Quote(market_hash_name=name, source="csfloat", currency="USD", price=prices[0], median=median,
                     listings=len(prices), version=phase,
                     url=f"https://csfloat.com/item/{first_id}" if first_id else
                     f"https://csfloat.com/search?market_hash_name={name}")
