"""Market metrics for one item, computed from REAL stored data.

Metric sources, in order of preference:
1. The bot's own snapshot history (`price_snapshots`), once it covers the requested window.
2. Sales statistics published by the platform (Skinport /sales/history: 24h/7d/30d/90d).
Every metric carries the "basis" it was computed with, so explanations stay transparent.
Without enough data a metric stays None (it is never filled with made-up values).
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from ..utils.text import pct_change

DAY = 86400.0


@dataclass
class ItemData:
    name: str                       # market_hash_name (+ phase)
    short: str
    item_type: str
    currency: str
    reference_source: str
    latest: dict = field(default_factory=dict)   # source -> {price, median, bid, volume_24h, listings, ts, url}
    stats: dict = field(default_factory=dict)    # source -> {window -> {min,max,avg,median,volume}}
    series: dict = field(default_factory=dict)   # source -> [(ts, price)] ascending
    news: list = field(default_factory=list)     # [(ts, title, category, important)]
    collection: str | None = None
    rarity: str | None = None


@dataclass
class Metrics:
    name: str
    short: str
    currency: str
    source: str                          # source of the current price
    price: float | None = None
    url: str | None = None
    price_ts: float | None = None
    change: dict = field(default_factory=dict)   # "24h"/"7d"/"30d" -> (pct, base)
    avg30: float | None = None
    avg30_basis: str = ""
    median30: float | None = None
    min90: float | None = None
    max90: float | None = None
    min_max_basis: str = ""
    volume_24h: int | None = None
    volume_7d: int | None = None
    volume_30d: int | None = None
    volume_basis: str = ""
    volume_trend: float | None = None    # 24h sales/day ratio vs the 7d daily average
    volume_label: str = "n/a"
    liquidity: float = 0.0               # 0..1
    liquidity_label: str = "n/a"
    spread_pct: float | None = None
    spread_basis: str = ""
    volatility: float | None = None      # (max30-min30)/median30 of sales
    platforms: list = field(default_factory=list)  # [(source, price, url, age_s)] sorted by price
    cheapest_other: tuple | None = None
    cross_diff_pct: float | None = None  # current price vs the cheapest other platform (+ = more expensive)
    others_median: float | None = None
    trend_label: str = "n/a"             # BULLISH | BEARISH | STABLE | n/a
    trend_7_30: float | None = None
    trend_30_90: float | None = None
    anomaly: float = 0.0                 # discount not confirmed by another platform (0..1)
    manipulation: float = 0.0            # 0..1
    manipulation_notes: list = field(default_factory=list)
    stale: bool = False
    series_points: int = 0


def _value_at(series: list[tuple[float, float]], ts: float, tolerance: float) -> float | None:
    """Price of the snapshot closest to `ts` within `tolerance` seconds."""
    best = None
    best_d = tolerance
    for t, p in series:
        d = abs(t - ts)
        if d <= best_d and p is not None:
            best, best_d = p, d
    return best


def compute_metrics(d: ItemData, cfg: dict | None = None) -> Metrics:
    cfg = cfg or {}
    now = time.time()
    max_age = float(cfg.get("max_price_age_s", 6 * 3600))
    target_daily = float(cfg.get("liquidity_target_daily_sales", 40))

    # --------------------------------------------------- current price
    ref = d.reference_source if d.reference_source in d.latest else None
    if ref is None and d.latest:
        ref = min(d.latest, key=lambda s: (d.latest[s].get("price") is None, -(d.latest[s].get("ts") or 0)))
    m = Metrics(name=d.name, short=d.short, currency=d.currency, source=ref or "n/a")
    if ref:
        lt = d.latest[ref]
        m.price, m.url, m.price_ts = lt.get("price"), lt.get("url"), lt.get("ts")
        m.stale = bool(m.price_ts and now - m.price_ts > max_age)

    # ------------------------------------------------- sales statistics
    st = d.stats.get("skinport") or next(iter(d.stats.values()), {}) if d.stats else {}
    st_src = "skinport" if "skinport" in d.stats else (next(iter(d.stats), "") if d.stats else "")
    w24, w7, w30, w90 = (st.get(k) or {} for k in ("24h", "7d", "30d", "90d"))

    # -------------------------------------------------------- changes
    series = d.series.get(m.source, [])
    m.series_points = len(series)
    if series and m.price is not None:
        first_ts = series[0][0]
        for label, secs in (("24h", DAY), ("7d", 7 * DAY), ("30d", 30 * DAY)):
            if first_ts <= now - secs * 0.9:
                old = _value_at(series, now - secs, secs * 0.25)
                ch = pct_change(m.price, old)
                if ch is not None:
                    m.change[label] = (ch, f"own history {m.source}")
    # Fallback using the platform's real sales (current price vs the window's median)
    if m.price is not None:
        for label, w in (("24h", w24), ("7d", w7), ("30d", w30)):
            if label not in m.change and w.get("median") and (w.get("volume") or 0) >= 1:
                ch = pct_change(m.price, w["median"])
                if ch is not None:
                    m.change[label] = (ch, f"vs median sale {label} {st_src}")

    # ------------------------------------------------ averages and extremes
    pts30 = [p for t, p in series if t >= now - 30 * DAY and p is not None]
    span_days = (series[-1][0] - series[0][0]) / DAY if len(series) > 1 else 0
    if len(pts30) >= 20 and span_days >= 20:
        m.avg30, m.median30, m.avg30_basis = statistics.fmean(pts30), statistics.median(pts30), f"own history {m.source}"
    elif w30.get("median"):
        m.avg30, m.median30, m.avg30_basis = w30.get("avg"), w30.get("median"), f"30d sales {st_src}"
    pts90 = [p for t, p in series if t >= now - 90 * DAY and p is not None]
    if span_days >= 80 and pts90:
        m.min90, m.max90, m.min_max_basis = min(pts90), max(pts90), f"own history {m.source}"
    elif w90.get("min") is not None:
        m.min90, m.max90, m.min_max_basis = w90.get("min"), w90.get("max"), f"90d sales {st_src}"

    # ---------------------------------------------------------- volume
    steam_vol = (d.latest.get("steam") or {}).get("volume_24h")
    m.volume_24h = w24.get("volume") if w24 else None
    m.volume_7d = w7.get("volume") if w7 else None
    m.volume_30d = w30.get("volume") if w30 else None
    m.volume_basis = st_src
    if steam_vol is not None:
        m.volume_24h = (m.volume_24h or 0) + steam_vol
        m.volume_basis = f"{st_src}+steam" if st_src else "steam"
    daily7 = (m.volume_7d or 0) / 7 if m.volume_7d else None
    if daily7 and w24:
        m.volume_trend = (w24.get("volume") or 0) / daily7
        m.volume_label = ("RISING" if m.volume_trend >= 1.6 else "FALLING" if m.volume_trend <= 0.5
                          else "STABLE")
    daily = max(daily7 or 0, (m.volume_30d or 0) / 30) + (steam_vol or 0)
    m.liquidity = min(1.0, math.log10(1 + daily) / math.log10(1 + target_daily)) if daily > 0 else 0.0
    m.liquidity_label = "HIGH" if m.liquidity >= 0.7 else "MEDIUM" if m.liquidity >= 0.35 else "LOW"

    # ---------------------------------------------------------- spread
    bids = [(s, v.get("price"), v.get("bid")) for s, v in d.latest.items() if v.get("bid") and v.get("price")]
    if bids:
        s, ask, bid = bids[0]
        m.spread_pct, m.spread_basis = (ask - bid) / ask * 100, f"ask/bid {s}"
    elif m.price and w7.get("median"):
        m.spread_pct, m.spread_basis = (m.price - w7["median"]) / m.price * 100, "ask vs 7d median sale"

    if w30.get("median") and w30.get("min") is not None and w30.get("max") is not None:
        m.volatility = (w30["max"] - w30["min"]) / w30["median"]

    # ---------------------------------------------------- cross-platform
    plats = []
    for s, v in d.latest.items():
        if v.get("price") is None:
            continue
        age = now - (v.get("ts") or now)
        if age <= max_age * 4:
            plats.append((s, v["price"], v.get("url"), age))
    plats.sort(key=lambda x: x[1])
    m.platforms = plats
    others = [p for p in plats if p[0] != m.source]
    if others and m.price:
        m.cheapest_other = others[0]
        m.cross_diff_pct = (m.price - others[0][1]) / others[0][1] * 100
        m.others_median = statistics.median([p[1] for p in others])

    # ------------------------------------------------------------ trend
    med7, med30, med90 = w7.get("median"), w30.get("median"), w90.get("median")
    if med7 and med30:
        m.trend_7_30 = (med7 - med30) / med30 * 100
    if med30 and med90:
        m.trend_30_90 = (med30 - med90) / med90 * 100
    if m.trend_7_30 is not None:
        t = m.trend_7_30 + 0.5 * (m.trend_30_90 or 0)
        m.trend_label = "BULLISH" if t >= 5 else "BEARISH" if t <= -5 else "STABLE"

    # ------------------------------------------- manipulation signals
    notes, risk = [], 0.0
    vol30 = m.volume_30d or 0
    if vol30 < float(cfg.get("thin_market_sales_30d", 10)):
        risk += 0.35
        notes.append(f"thin market ({vol30} sales/30d)")
    if med30 and w30.get("max") and w30["max"] / med30 > 3:
        risk += 0.2
        notes.append(f"outlier sales (30d max {w30['max'] / med30:.1f}x the median)")
    if med7 and med30 and med7 > med30 * 1.3 and (m.volume_7d or 0) < 15:
        risk += 0.25
        notes.append("sharp rise on low volume (possible pump)")
    lst = (d.latest.get(m.source) or {}).get("listings")
    if m.price and med30 and lst is not None and lst <= 2 and m.price < med30 * 0.7:
        risk += 0.25
        notes.append(f"very low price with only {lst} listing(s): may be a one-off/outlier listing")
    if m.price and m.others_median and m.price < m.others_median * 0.6:
        risk += 0.2
        notes.append("price far below other platforms (verify the listing)")
    ref_med = m.median30 or med7
    if m.price and ref_med and not m.others_median:
        m.anomaly = max(0.0, 1 - m.price / ref_med)
        if m.anomaly >= 0.25:
            risk += min(0.4, 0.15 + (m.anomaly - 0.25))
            notes.append(f"{m.anomaly * 100:.0f}% discount vs median not confirmed on another platform: "
                         "may be an outlier listing (float/pattern) or an error")
    elif m.price and m.others_median:
        m.anomaly = max(0.0, 1 - m.price / m.others_median)
    if m.stale:
        risk += 0.1
        notes.append("stale price data")
    m.manipulation = min(1.0, risk)
    m.manipulation_notes = notes
    return m
