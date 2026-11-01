"""Opportunity scoring engine (0-100) with a transparent explanation.

Each component yields a 0..1 value multiplied by its weight (scoring.yaml).
The sum is normalised to 100 and the manipulation-risk penalty is subtracted.
It is NOT a prediction: it summarises real indicators to decide what is worth checking.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .metrics import Metrics

DEFAULT_WEIGHTS = {"price_drop": 25, "volume": 15, "liquidity": 15, "trend": 15, "cross_platform": 15,
                   "news_context": 10}
DEFAULT_THRESHOLDS = {"drop_24h": 5.0, "drop_7d": 10.0, "drop_30d": 15.0, "below_avg30": 8.0,
                      "near_min90": 5.0, "volume_spike": 1.6}
DEFAULT_LABELS = {"excellent": 80, "interesting": 65, "watch": 50}
COMPONENT_NAMES = {"price_drop": "Price drop", "volume": "Volume", "liquidity": "Liquidity",
                   "trend": "Historical trend", "cross_platform": "Cross-platform",
                   "news_context": "News context"}


@dataclass
class Component:
    key: str
    value: float          # 0..1
    points: float
    max_points: float
    explanation: str


@dataclass
class ScoreResult:
    score: float
    label: str
    risk: str
    components: list[Component] = field(default_factory=list)
    penalty: float = 0.0
    penalty_notes: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)   # triggered drop signals
    reason: str = ""
    eligible: bool = False
    break_even_pct: float | None = None

    def as_dict(self) -> dict:
        return {"score": round(self.score, 1), "label": self.label, "risk": self.risk, "penalty": round(self.penalty, 1),
                "penalty_notes": self.penalty_notes, "signals": self.signals, "reason": self.reason,
                "break_even_pct": self.break_even_pct,
                "components": [{"key": c.key, "points": round(c.points, 1), "max": c.max_points,
                                "explanation": c.explanation} for c in self.components]}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def risk_level(m: Metrics) -> tuple[str, float]:
    vol = min(1.0, (m.volatility or 0) / 1.5)
    r = 0.5 * m.manipulation + 0.3 * (1 - m.liquidity) + 0.2 * vol + 0.6 * max(0.0, m.anomaly - 0.15)
    return ("HIGH" if r >= 0.55 else "MEDIUM" if r >= 0.3 else "LOW"), r


def news_component(news: list, now: float | None = None) -> tuple[float, str]:
    """No relevant news → stable context (1.0). Related news → uncertainty."""
    now = now or time.time()
    recent = [n for n in news if now - n[0] <= 7 * 86400]
    valve = [n for n in recent if n[2] in ("actualizacion", "parche", "cajas", "operacion", "economia")
             and now - n[0] <= 3 * 86400]
    if valve:
        return 0.2, f"recent update affecting the market: \"{valve[0][1][:60]}\" (more uncertainty)"
    if recent:
        return 0.5, f"{len(recent)} related news item(s) in 7 days: \"{recent[0][1][:60]}\""
    return 1.0, "no recent news changing the context"


def score_drop(m: Metrics, scoring: dict | None = None, news: list | None = None,
               seller_fee: float | None = None) -> ScoreResult:
    scoring = scoring or {}
    w = {**DEFAULT_WEIGHTS, **(scoring.get("weights") or {})}
    th = {**DEFAULT_THRESHOLDS, **(scoring.get("thresholds") or {})}
    labels = {**DEFAULT_LABELS, **(scoring.get("labels") or {})}
    max_penalty = float(scoring.get("manipulation_max_penalty", 20))
    comps: list[Component] = []
    signals: list[str] = []

    # --------------------------------------------------------- price drop
    drop_scores = []
    for label, key in (("24h", "drop_24h"), ("7d", "drop_7d"), ("30d", "drop_30d")):
        ch = m.change.get(label)
        if ch and ch[0] <= -th[key]:
            if label == "30d" and ch[1].startswith("vs median"):
                continue  # same signal as "below the 30d median" (evaluated below)
            if ch[1].startswith("vs median"):
                signals.append(f"{-ch[0]:.1f}% below the {label} median sale ({ch[1].split()[-1]})")
            else:
                signals.append(f"{label} drop {ch[0]:+.1f}% ({ch[1]})")
            drop_scores.append(_clip(-ch[0] / (th[key] * 2.5)))
    if m.price and m.median30:
        below = (m.median30 - m.price) / m.median30 * 100
        if below >= th["below_avg30"]:
            signals.append(f"{below:.1f}% below the 30d median ({m.avg30_basis})")
            drop_scores.append(_clip(below / (th["below_avg30"] * 3)))
    if m.price and m.min90:
        near = (m.price - m.min90) / m.min90 * 100
        if near <= th["near_min90"]:
            signals.append(f"near the 90d low ({near:+.1f}%)")
            drop_scores.append(0.8 if near > 0 else 1.0)
    drop_v = max(drop_scores) * 0.75 + (sum(drop_scores) / len(drop_scores)) * 0.25 if drop_scores else 0.0
    comps.append(Component("price_drop", drop_v, drop_v * w["price_drop"], w["price_drop"],
                           "; ".join(signals) if signals else "no significant drop"))

    # ------------------------------------------------------------- volume
    vol_v, vol_e = 0.0, "no volume data"
    if m.volume_7d is not None:
        adequacy = _clip((m.volume_7d / 7) / 5)  # ≥5 sales/day = enough volume
        trend = 0.5 if m.volume_trend is None else _clip(0.4 + (m.volume_trend - 1) * 0.5)
        vol_v = 0.6 * adequacy + 0.4 * trend
        vol_e = f"{m.volume_7d} sales/7d, trend {m.volume_label}"
        if m.volume_trend and m.volume_trend >= th["volume_spike"]:
            signals.append(f"volume rising (x{m.volume_trend:.1f} vs 7d average)")
    comps.append(Component("volume", vol_v, vol_v * w["volume"], w["volume"], vol_e))

    # ---------------------------------------------------------- liquidity
    comps.append(Component("liquidity", m.liquidity, m.liquidity * w["liquidity"], w["liquidity"],
                           f"liquidity {m.liquidity_label} ({m.liquidity:.2f})"))

    # -------------------------------------------------------------- trend
    if m.trend_30_90 is None and m.trend_7_30 is None:
        tr_v, tr_e = 0.4, "not enough history to judge the trend"
    else:
        long = m.trend_30_90 or 0.0
        short = m.trend_7_30 or 0.0
        if long >= -3 and short > -15:
            tr_v, tr_e = 1.0, f"one-off dip in a healthy long-term trend (30d vs 90d {long:+.1f}%)"
        elif long >= -10:
            tr_v, tr_e = 0.55, f"weak long-term trend (30d vs 90d {long:+.1f}%, 7d vs 30d {short:+.1f}%)"
        else:
            tr_v, tr_e = 0.15, f"prolonged downtrend (30d vs 90d {long:+.1f}%): may keep falling"
    comps.append(Component("trend", tr_v, tr_v * w["trend"], w["trend"], tr_e))

    # ----------------------------------------------------- cross-platform
    if m.cross_diff_pct is None:
        cp_v, cp_e = 0.3, "only one platform with a recent price"
    elif m.cross_diff_pct <= 0:
        cp_v = _clip(-m.cross_diff_pct / 15)
        cp_e = f"{-m.cross_diff_pct:.1f}% cheaper than {m.cheapest_other[0]}"
    else:
        cp_v = 0.0
        cp_e = f"{m.cheapest_other[0]} is {m.cross_diff_pct:.1f}% cheaper"
    comps.append(Component("cross_platform", cp_v, cp_v * w["cross_platform"], w["cross_platform"], cp_e))

    # --------------------------------------------------------------- news
    nv, ne = news_component(news or [])
    comps.append(Component("news_context", nv, nv * w["news_context"], w["news_context"], ne))

    total_w = sum(w.values()) or 1
    raw = sum(c.points for c in comps) / total_w * 100
    penalty = m.manipulation * max_penalty
    score = max(0.0, min(100.0, raw - penalty))
    risk, _ = risk_level(m)
    if risk == "HIGH" and score >= labels["watch"]:
        label = "HIGH RISK"
    elif score >= labels["excellent"] and risk != "HIGH":
        label = "EXCELLENT"
    elif score >= labels["interesting"]:
        label = "INTERESTING"
    elif score >= labels["watch"]:
        label = "WATCH"
    else:
        label = "DISCARDED"

    res = ScoreResult(score=score, label=label, risk=risk, components=comps, penalty=penalty,
                      penalty_notes=list(m.manipulation_notes), signals=signals,
                      eligible=bool(drop_scores) and m.price is not None and not m.stale)
    if seller_fee:
        res.break_even_pct = round((1 / (1 - seller_fee) - 1) * 100, 1)
    res.reason = build_reason(m, res)
    return res


def build_reason(m: Metrics, r: ScoreResult) -> str:
    parts = []
    if r.signals:
        parts.append(r.signals[0])
    if m.liquidity >= 0.7:
        parts.append("high liquidity")
    elif m.liquidity < 0.35:
        parts.append("low liquidity")
    comp = {c.key: c for c in r.components}
    if comp["trend"].value >= 1:
        parts.append("stable long-term trend")
    elif comp["trend"].value <= 0.2:
        parts.append("prolonged downtrend")
    if comp["cross_platform"].value >= 0.5:
        parts.append(comp["cross_platform"].explanation)
    if r.penalty_notes:
        parts.append("caution: " + r.penalty_notes[0])
    return "; ".join(parts) or "no notable signals"
