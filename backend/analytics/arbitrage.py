"""Cross-platform comparison and potentially exploitable price gaps (arbitrage).

The calculation is conservative:
- cost    = lowest ask on A × (1 + A's buyer fee)
- revenue = (lowest ask on B − undercut to sell) × (1 − B's seller fee)
  using the LOWER of B's ask and B's median sale when available.
Warnings are added for non-cashable balances (Steam), trade holds and low liquidity.
No result is a guaranteed profit: prices move, listings disappear and each specific
item (float/pattern/stickers) can be worth a different amount.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..sources.market.base import Fees
from .metrics import Metrics


@dataclass
class ArbResult:
    buy_source: str
    sell_source: str
    buy_price: float
    sell_price: float
    cost: float
    revenue: float
    profit: float
    profit_pct: float
    gross_pct: float
    warnings: list = field(default_factory=list)
    buy_url: str | None = None
    score: float = 0.0
    label: str = ""

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def compute_arbitrage(m: Metrics, latest: dict, fees: dict[str, Fees], cfg: dict | None = None) -> list[ArbResult]:
    cfg = cfg or {}
    undercut = float(cfg.get("undercut_pct", 1.0)) / 100
    min_pct = float(cfg.get("min_profit_pct", 5.0))
    out: list[ArbResult] = []
    plats = {p[0]: p for p in m.platforms}
    for a, (_, pa, url_a, age_a) in plats.items():
        for b, (_, pb, _url_b, age_b) in plats.items():
            if a == b:
                continue
            fa, fb = fees.get(a, Fees()), fees.get(b, Fees())
            lb = latest.get(b) or {}
            sell_ref = pb
            if lb.get("median"):
                sell_ref = min(pb, lb["median"])
            sell_price = sell_ref * (1 - undercut)
            cost = pa * (1 + fa.buyer_fee)
            revenue = sell_price * (1 - fb.seller_fee)
            profit = revenue - cost
            pct = profit / cost * 100 if cost else 0
            if pct < min_pct:
                continue
            warns = []
            if not fb.cashout:
                warns.append(f"{b} balance cannot be cashed out")
            if max(fa.trade_hold_days, fb.trade_hold_days):
                warns.append(f"trade hold ~{max(fa.trade_hold_days, fb.trade_hold_days)} days")
            if m.liquidity < 0.35:
                warns.append("low liquidity: may take time to sell")
            if max(age_a, age_b) > 3600:
                warns.append(f"price is {int(max(age_a, age_b) // 60)} min old")
            if m.manipulation >= 0.4:
                warns.append("outlier price signals")
            if pa < (m.others_median or pa) * 0.6:
                warns.append("the cheap listing may be a worse float/pattern or an error")
            gross = (pb - pa) / pa * 100
            res = ArbResult(buy_source=a, sell_source=b, buy_price=pa, sell_price=sell_price, cost=cost,
                            revenue=revenue, profit=profit, profit_pct=pct, gross_pct=gross, warnings=warns,
                            buy_url=url_a)
            # score: net margin (up to 20% = max), liquidity, penalty per warning
            res.score = max(0.0, min(100.0, min(1.0, pct / 20) * 55 + m.liquidity * 30 + (15 if fb.cashout else 5)
                                     - 8 * len([w for w in warns if "trade hold" not in w])))
            res.label = ("HIGH RISK" if m.manipulation >= 0.5 or m.liquidity < 0.2 else
                         "EXCELLENT" if res.score >= 80 else "INTERESTING" if res.score >= 65 else "WATCH")
            out.append(res)
    out.sort(key=lambda r: r.profit_pct, reverse=True)
    return out


def net_if_sold(m: Metrics, fees: dict[str, Fees]) -> list[tuple[str, float, float, bool]]:
    """For !skincompare: (source, price, net if you sell there, cashable)."""
    out = []
    for s, price, _url, _age in m.platforms:
        f = fees.get(s, Fees())
        out.append((s, price, price * (1 - f.seller_fee), f.cashout))
    return out
