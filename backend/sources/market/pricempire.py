"""Pricempire (paid API: https://pricempire.com/api).

The exact format depends on the subscribed API plan/version, so the URL, the parameters
and the provider to read are configurable in sources.yaml. The parser accepts both publicly
documented response shapes (dict keyed by name, or a list of objects with `prices`).
STATUS: implemented but NOT verified against the real API (requires a paid key).
"""
from __future__ import annotations

from .base import MarketAdapter, Quote


class Pricempire(MarketAdapter):
    name = "pricempire"
    supports_bulk = True

    @property
    def configured(self) -> bool:
        return bool(self.conf.get("api_key"))

    async def fetch_bulk(self) -> list[Quote]:
        url = self.conf.get("url", "https://api.pricempire.com/v4/paid/items/prices")
        params = dict(self.conf.get("params") or {"app_id": 730, "currency": "USD", "sources": "buff163"})
        headers = {"Authorization": f"Bearer {self.conf.get('api_key')}"}
        data = await self.http.get_json(self.name, url, params=params, headers=headers)
        return self.parse(data, str(self.conf.get("provider", "buff163")), str(params.get("currency", "USD")),
                          float(self.conf.get("price_divisor", 100)))

    @staticmethod
    def parse(data, provider: str, currency: str, divisor: float = 100) -> list[Quote]:
        out = []
        if isinstance(data, dict):
            data = data.get("items", data)
        if isinstance(data, dict):
            rows = [{"market_hash_name": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
        else:
            rows = data or []
        for r in rows:
            name = r.get("market_hash_name")
            entry = None
            if isinstance(r.get("prices"), list):
                entry = next((p for p in r["prices"] if p.get("provider_key") == provider), None)
            elif isinstance(r.get(provider), dict):
                entry = r[provider]
            if not name or not entry or entry.get("price") in (None, 0):
                continue
            out.append(Quote(market_hash_name=name, source="pricempire", currency=currency,
                             price=float(entry["price"]) / divisor, listings=entry.get("count")))
        return out
