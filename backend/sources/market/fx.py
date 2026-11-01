"""Official European Central Bank exchange rates (daily, no key)."""
from __future__ import annotations

import logging
import re
import time

from ...core.http import HttpClient

log = logging.getLogger("cs2.fx")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class FxRates:
    def __init__(self, http: HttpClient, base: str = "EUR") -> None:
        self.http = http
        self.base = base.upper()
        self.rates: dict[str, float] = {"EUR": 1.0}  # units per 1 EUR
        self.updated_at: float | None = None
        self.date: str | None = None

    async def refresh(self) -> None:
        r = await self.http.get("ecb", ECB_URL, cache_ttl=3600)
        rates, date = self.parse(r.text)
        if rates:
            self.rates = {"EUR": 1.0, **rates}
            self.updated_at = time.time()
            self.date = date

    @staticmethod
    def parse(xml: str) -> tuple[dict[str, float], str | None]:
        rates = {m.group(1): float(m.group(2)) for m in re.finditer(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml)}
        d = re.search(r"time=['\"](\d{4}-\d\d-\d\d)['\"]", xml)
        return rates, d.group(1) if d else None

    def convert(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        currency = currency.upper()
        if currency == self.base:
            return amount
        if currency not in self.rates or self.base not in self.rates:
            return None  # no rate available: never invent a conversion
        eur = amount / self.rates[currency]
        return eur * self.rates[self.base]
