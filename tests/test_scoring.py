"""Metrics, scoring and arbitrage engine. ALL market data here is SYNTHETIC."""
import time

from backend.analytics.arbitrage import compute_arbitrage
from backend.analytics.metrics import DAY, ItemData, compute_metrics
from backend.analytics.scoring import score_drop
from backend.sources.market.base import Fees

NOW = time.time()


def stats(med7=20.0, med30=20.0, med90=20.0, vol24=30, vol7=210, vol30=900, max30=None, min90=None):
    return {"24h": {"min": med7 * .9, "max": med7 * 1.1, "avg": med7, "median": med7, "volume": vol24},
            "7d": {"min": med7 * .9, "max": med7 * 1.2, "avg": med7, "median": med7, "volume": vol7},
            "30d": {"min": med30 * .85, "max": max30 or med30 * 1.2, "avg": med30, "median": med30, "volume": vol30},
            "90d": {"min": min90 or med90 * .8, "max": med90 * 1.3, "avg": med90, "median": med90, "volume": vol30 * 3}}


def data(price, st, others=None, series=None, listings=50, news=None):
    latest = {"skinport": {"price": price, "ts": NOW, "url": "https://example/sp", "listings": listings}}
    for s, p in (others or {}).items():
        latest[s] = {"price": p, "ts": NOW, "url": f"https://example/{s}"}
    return ItemData(name="SYNTH | Test (Field-Tested)", short="SYNTH (FT)", item_type="weapon", currency="EUR",
                    reference_source="skinport", latest=latest, stats={"skinport": st},
                    series={"skinport": series or []}, news=news or [])


def test_good_dip_scores_high_and_explains():
    d = data(17.0, stats(med7=20, med30=20, med90=19.5), others={"steam": 21.0, "csfloat": 19.0})
    m = compute_metrics(d)
    r = score_drop(m)
    assert r.eligible and r.score >= 65 and r.label in ("EXCELLENT", "INTERESTING")
    assert any("median" in s for s in r.signals)
    assert {c.key for c in r.components} == {"price_drop", "volume", "liquidity", "trend", "cross_platform",
                                              "news_context"}
    from backend.analytics.scoring import DEFAULT_WEIGHTS
    assert sum(c.max_points for c in r.components) == sum(DEFAULT_WEIGHTS.values())  # normalised to 100
    assert abs(r.score - sum(c.points for c in r.components) / sum(DEFAULT_WEIGHTS.values()) * 100 + r.penalty) < 1e-6
    assert r.risk in ("LOW", "MEDIUM")


def test_no_drop_not_eligible():
    r = score_drop(compute_metrics(data(20.0, stats())))
    assert not r.eligible


def test_thin_market_and_unconfirmed_extreme_drop_are_high_risk():
    d = data(8.0, stats(med7=20, med30=20, vol24=0, vol7=2, vol30=4, max30=90), listings=1)
    m = compute_metrics(d)
    r = score_drop(m)
    assert m.manipulation >= 0.6 and r.risk == "HIGH"
    assert r.penalty > 10 and r.penalty_notes
    assert r.label in ("HIGH RISK", "DISCARDED")


def test_structural_downtrend_penalised_vs_dip():
    dip = score_drop(compute_metrics(data(17.0, stats(med7=20, med30=20, med90=20))))
    falling = score_drop(compute_metrics(data(17.0, stats(med7=18, med30=20, med90=30))))
    t_dip = next(c for c in dip.components if c.key == "trend").value
    t_fall = next(c for c in falling.components if c.key == "trend").value
    assert t_dip > t_fall and dip.score > falling.score


def test_own_history_preferred_over_sales_stats():
    series = [(NOW - i * 3600, 20.0) for i in range(24 * 35, 0, -6)]
    m = compute_metrics(data(18.0, stats(med30=30), series=series))
    assert m.change["24h"][1].startswith("own history")
    assert m.avg30_basis.startswith("own history") and abs(m.median30 - 20.0) < 1e-9


def test_weights_configurable():
    d = data(17.0, stats(), others={"steam": 25.0})
    base = score_drop(compute_metrics(d))
    heavy = score_drop(compute_metrics(d), {"weights": {"cross_platform": 60}})
    assert heavy.score != base.score


def test_news_context_reduces_score():
    d1 = data(17.0, stats())
    d2 = data(17.0, stats(), news=[(NOW - 3600, "Counter-Strike 2 Update", "actualizacion", True)])
    assert score_drop(compute_metrics(d1), news=d1.news).score > score_drop(compute_metrics(d2), news=d2.news).score


def test_arbitrage_accounts_fees_and_warnings():
    d = data(10.0, stats(med7=12, med30=12, med90=12), others={"steam": 14.0, "csfloat": 13.0})
    m = compute_metrics(d)
    fees = {"skinport": Fees(0.08, 0, True, 7), "steam": Fees(0.1304, 0, False, 7), "csfloat": Fees(0.02, 0, True, 7)}
    res = compute_arbitrage(m, d.latest, fees, {"min_profit_pct": 5})
    best = res[0]
    assert best.buy_source == "skinport"
    for r in res:
        assert r.profit_pct < r.gross_pct  # net is always below gross
    steam = next(r for r in res if r.sell_source == "steam")
    assert any("cannot be cashed out" in w for w in steam.warnings)
    # selling on CSFloat: 13 * 0.99 * 0.98 = 12.61 → +26%
    cs = next(r for r in res if r.sell_source == "csfloat")
    assert abs(cs.revenue - 13 * 0.99 * 0.98) < 1e-6


def test_no_arbitrage_when_fees_eat_margin():
    d = data(10.0, stats(), others={"steam": 11.0})
    m = compute_metrics(d)
    assert compute_arbitrage(m, d.latest, {"steam": Fees(0.1304, 0, False)}, {"min_profit_pct": 5}) == []


def test_stale_price_is_flagged():
    d = data(17.0, stats())
    d.latest["skinport"]["ts"] = NOW - 2 * DAY
    m = compute_metrics(d, {"max_price_age_s": 3600})
    assert m.stale and not score_drop(m).eligible
