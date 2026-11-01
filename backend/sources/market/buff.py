"""Buff163 — ONLY with authorised access (the account holder's own session).

Buff offers no documented public API. This adapter uses the listing endpoint its website uses
(`/api/market/goods`) with the user's own session cookie (`BUFF_COOKIE`). If the cookie
is invalid or Buff refuses access, the source is marked as blocked and not retried.
Prices in CNY; includes the best buy order (bid) → real spread.
"""
from __future__ import annotations

from .base import MarketAdapter, Quote, parse_price

API = "https://buff.163.com/api/market/goods"


class Buff163(MarketAdapter):
    name = "buff163"
    supports_bulk = True

    @property
    def configured(self) -> bool:
        return bool(self.conf.get("cookie"))

    async def fetch_bulk(self) -> list[Quote]:
        out: list[Quote] = []
        for page in range(1, int(self.conf.get("pages", 5)) + 1):
            data = await self.http.get_json(self.name, API, params={
                "game": "csgo", "page_num": page, "page_size": 80, "sort_by": "sell_num.desc"})
            items = self.parse_goods(data)
            out.extend(items)
            if not items:
                break
        return out

    @staticmethod
    def parse_goods(data: dict) -> list[Quote]:
        if not isinstance(data, dict) or data.get("code") != "OK":
            return []
        out = []
        for r in (data.get("data") or {}).get("items", []) or []:
            name = r.get("market_hash_name")
            if not name:
                continue
            out.append(Quote(market_hash_name=name, source="buff163", currency="CNY",
                             price=parse_price(r.get("sell_min_price")), bid=parse_price(r.get("buy_max_price")),
                             listings=r.get("sell_num"), url=f"https://buff.163.com/goods/{r.get('id')}"))
        return out
