"""Common interface of the market adapters.

Every platform has its own adapter; when an API changes, only that adapter needs updating.
Adapters return prices in the source's ORIGINAL CURRENCY; conversion to the base currency
happens in the market service using official ECB rates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...core.config import Config
from ...core.http import HttpClient


@dataclass
class Quote:
    market_hash_name: str
    source: str
    currency: str
    price: float | None = None        # lowest sell price (ask)
    median: float | None = None       # median (sales or listings, depending on the source)
    bid: float | None = None          # best buy order
    volume_24h: int | None = None     # sales in 24 h (if the source provides it)
    listings: int | None = None       # units for sale
    url: str | None = None
    version: str | None = None        # phase / variant (Doppler Phase 2, Ruby…)


@dataclass
class SalesStats:
    market_hash_name: str
    source: str
    currency: str
    windows: dict = field(default_factory=dict)  # {"24h": {min,max,avg,median,volume}, "7d": ..., ...}
    url: str | None = None
    version: str | None = None


@dataclass
class Fees:
    seller_fee: float = 0.0        # fraction the platform keeps from the seller
    buyer_fee: float = 0.0         # surcharge paid by the buyer
    cashout: bool = True           # can the balance be cashed out as real money?
    trade_hold_days: int = 7       # trade hold (Steam trade protection)
    notes: str = ""


class MarketAdapter:
    name = "base"
    supports_bulk = False
    supports_item = False
    supports_stats = False

    def __init__(self, cfg: Config, http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http

    @property
    def conf(self) -> dict:
        return self.cfg.source(self.name)

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False)) and self.configured

    @property
    def configured(self) -> bool:
        """False when a mandatory credential is missing."""
        return True

    def fees(self) -> Fees:
        f = self.conf.get("fees", {}) or {}
        return Fees(seller_fee=float(f.get("seller", 0)), buyer_fee=float(f.get("buyer", 0)),
                    cashout=bool(f.get("cashout", True)), trade_hold_days=int(f.get("trade_hold_days", 7)),
                    notes=str(f.get("notes", "")))

    async def fetch_bulk(self) -> list[Quote]:
        raise NotImplementedError

    async def fetch_item(self, market_hash_name: str) -> Quote | None:
        raise NotImplementedError

    async def fetch_stats_bulk(self) -> list[SalesStats]:
        raise NotImplementedError


def parse_price(text: str | None) -> float | None:
    """'28,37€' → 28.37 · '$1,234.56' → 1234.56 · '1.234,56 zł' → 1234.56 · '28,--€' → 28.0"""
    if not text:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(text)).replace("--", "00").strip(".,")
    if not s or not re.search(r"\d", s):
        return None
    last_sep = max(s.rfind(","), s.rfind("."))
    if last_sep != -1 and len(s) - last_sep - 1 in (1, 2):
        integer = re.sub(r"[.,]", "", s[:last_sep])
        dec = s[last_sep + 1:]
        try:
            return float(f"{integer or '0'}.{dec}")
        except ValueError:
            return None
    try:
        return float(re.sub(r"[.,]", "", s))
    except ValueError:
        return None
