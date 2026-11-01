"""Skinport public API (https://docs.skinport.com).

- GET /v1/items         → every item: lowest price, suggested price, listing mean/median, quantity.
- GET /v1/sales/history → REAL sales statistics (24h/7d/30d/90d: min, max, avg, median, volume).
Both need no key, require `Accept-Encoding: br`, are cached 5 min at the origin and share a
limit of 8 requests / 5 min. The service queries them at most every 10 and 60 min.
"""
from __future__ import annotations

from .base import MarketAdapter, Quote, SalesStats

API = "https://api.skinport.com/v1"
WINDOWS = {"last_24_hours": "24h", "last_7_days": "7d", "last_30_days": "30d", "last_90_days": "90d"}


class Skinport(MarketAdapter):
    name = "skinport"
    supports_bulk = True
    supports_stats = True

    @property
    def currency(self) -> str:
        return str(self.conf.get("currency", "EUR")).upper()

    async def fetch_bulk(self) -> list[Quote]:
        data = await self.http.get_json(self.name, f"{API}/items",
                                        params={"app_id": 730, "currency": self.currency, "tradable": 0},
                                        headers={"Accept-Encoding": "br"})
        return self.parse_items(data)

    async def fetch_stats_bulk(self) -> list[SalesStats]:
        data = await self.http.get_json(self.name, f"{API}/sales/history",
                                        params={"app_id": 730, "currency": self.currency},
                                        headers={"Accept-Encoding": "br"})
        return self.parse_history(data)

    @staticmethod
    def parse_items(data: list) -> list[Quote]:
        out = []
        for r in data or []:
            name = r.get("market_hash_name")
            if not name:
                continue
            out.append(Quote(market_hash_name=name, source="skinport", currency=r.get("currency", "EUR"),
                             price=r.get("min_price"), median=r.get("median_price"),
                             listings=r.get("quantity"), url=r.get("item_page"), version=r.get("version")))
        return out

    @staticmethod
    def parse_history(data: list) -> list[SalesStats]:
        out = []
        for r in data or []:
            name = r.get("market_hash_name")
            if not name:
                continue
            windows = {}
            for k, w in WINDOWS.items():
                v = r.get(k) or {}
                windows[w] = {"min": v.get("min"), "max": v.get("max"), "avg": v.get("avg"),
                              "median": v.get("median"), "volume": v.get("volume") or 0}
            out.append(SalesStats(market_hash_name=name, source="skinport", currency=r.get("currency", "EUR"),
                                  windows=windows, url=r.get("item_page"), version=r.get("version")))
        return out
