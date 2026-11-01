"""Steam Community Market (public endpoints).

- `priceoverview`: lowest price, median and 24 h volume of ONE item. Strict limit
  (~20 requests/min per IP; beyond that it returns 429), so it is used through a queue and only for
  watched items, configured top items and opportunity candidates.
- `search/render?norender=1`: bulk listing (100 items per page) with sell price and
  number of listings. Also very limited: few pages, well spaced out.
- `pricehistory` requires a logged-in session and is not used.
"""
from __future__ import annotations

from urllib.parse import quote

from .base import MarketAdapter, Quote, parse_price

STEAM_CURRENCY = {"USD": 1, "GBP": 2, "EUR": 3, "CHF": 4, "RUB": 5, "PLN": 6, "BRL": 7, "CNY": 23}
BASE = "https://steamcommunity.com/market"


def listing_url(name: str) -> str:
    return f"{BASE}/listings/730/{quote(name, safe='')}"


class SteamMarket(MarketAdapter):
    name = "steam"
    supports_bulk = True
    supports_item = True

    @property
    def currency(self) -> str:
        return str(self.conf.get("currency", "EUR")).upper()

    async def fetch_item(self, market_hash_name: str) -> Quote | None:
        data = await self.http.get_json(self.name, f"{BASE}/priceoverview/", params={
            "appid": 730, "currency": STEAM_CURRENCY.get(self.currency, 3), "market_hash_name": market_hash_name})
        return self.parse_priceoverview(market_hash_name, data, self.currency)

    @staticmethod
    def parse_priceoverview(name: str, data: dict, currency: str) -> Quote | None:
        if not data or not data.get("success"):
            return None
        vol = data.get("volume")
        return Quote(market_hash_name=name, source="steam", currency=currency,
                     price=parse_price(data.get("lowest_price")), median=parse_price(data.get("median_price")),
                     volume_24h=int(str(vol).replace(",", "").replace(".", "")) if vol else 0,
                     url=listing_url(name))

    async def fetch_bulk(self) -> list[Quote]:
        pages = int(self.conf.get("bulk_pages", 2))
        out: list[Quote] = []
        for page in range(pages):
            data = await self.http.get_json(self.name, f"{BASE}/search/render/", params={
                "query": "", "start": page * 100, "count": 100, "search_descriptions": 0, "sort_column": "popular",
                "sort_dir": "desc", "appid": 730, "norender": 1, "currency": STEAM_CURRENCY.get(self.currency, 3)})
            out.extend(self.parse_search(data, self.currency))
        return out

    @staticmethod
    def parse_search(data: dict, currency: str) -> list[Quote]:
        out = []
        for r in (data or {}).get("results", []) or []:
            name = (r.get("asset_description") or {}).get("market_hash_name") or r.get("hash_name")
            if not name:
                continue
            cents = r.get("sell_price")
            out.append(Quote(market_hash_name=name, source="steam", currency=currency,
                             price=cents / 100 if isinstance(cents, (int, float)) and cents > 0 else None,
                             listings=r.get("sell_listings"), url=listing_url(name)))
        return out
