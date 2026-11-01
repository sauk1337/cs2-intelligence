"""Persistence, compaction, cleanup, backup and full analysis (SYNTHETIC data)."""
import gzip
import time

from sqlalchemy import func, select

from backend.database.maintenance import backup, cleanup, rollup_snapshots
from backend.database.models import MarketLatest, Opportunity, PriceSnapshot
from backend.sources.market.base import Quote, SalesStats


async def test_ingest_converts_currency_and_keeps_variants_apart(app):
    await app.market.ingest_quotes([
        Quote("★ Karambit | Doppler (Factory New)", "skinport", "EUR", price=800, version="Phase 2"),
        Quote("★ Karambit | Doppler (Factory New)", "skinport", "EUR", price=1500, version="Ruby"),
    ], snapshot=True)
    await app.market.ingest_quotes([Quote("★ Karambit | Doppler (Factory New)", "csfloat", "USD", price=880,
                                          version="Phase 2")], snapshot=True)
    p2 = app.market.items["★ Karambit | Doppler (Factory New) [Phase 2]"]
    ruby = app.market.items["★ Karambit | Doppler (Factory New) [Ruby]"]
    assert p2.id != ruby.id
    assert abs(app.market.latest[p2.id]["csfloat"]["price"] - 800.0) < 1e-6  # 880 USD / 1.10
    async with app.db.session() as s:
        assert (await s.execute(select(func.count(MarketLatest.id)))).scalar() == 3


async def test_unknown_currency_not_stored(app):
    n = await app.market.ingest_quotes([Quote("X Case", "buff163", "JPY", price=100)], snapshot=True)
    assert n == 0


async def test_rollup_and_cleanup(app):
    await app.market.ingest_quotes([Quote("AWP | Asiimov (Field-Tested)", "skinport", "EUR", price=50)], snapshot=True)
    item = app.market.items["AWP | Asiimov (Field-Tested)"].id
    old = time.time() - 20 * 86400
    async with app.db.session() as s:
        for h in range(5):
            s.add(PriceSnapshot(item_id=item, source="skinport", ts=old - old % 86400 + h * 3600, currency="EUR",
                                price=40 + h, granularity="raw"))
        s.add(PriceSnapshot(item_id=item, source="skinport", ts=time.time() - 500 * 86400, currency="EUR", price=1,
                            granularity="daily"))
    assert await rollup_snapshots(app.db, 7) == 1
    async with app.db.session() as s:
        rows = (await s.execute(select(PriceSnapshot).where(PriceSnapshot.granularity == "daily"))).scalars().all()
    assert any(r.price == 44 for r in rows)  # day close
    res = await cleanup(app.db, {"prices_days": 400})
    assert res["price_daily"] == 1


async def test_backup_creates_valid_gzip(app):
    p = await backup(app.db, app.backup_dir, keep=2)
    with gzip.open(p, "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")


async def test_full_analysis_stores_and_publishes(app):
    name = "Desert Eagle | Printstream (Field-Tested)"
    await app.market.ingest_quotes([Quote(name, "skinport", "EUR", price=24.0, listings=40)], snapshot=True)
    await app.market.ingest_quotes([Quote(name, "csfloat", "USD", price=31.0)], snapshot=True)
    w = {"min": 26, "max": 33, "avg": 29, "median": 29, "volume": 0}
    await app.market.ingest_stats([SalesStats(name, "skinport", "EUR", windows={
        "24h": {**w, "volume": 40}, "7d": {**w, "volume": 280}, "30d": {**w, "volume": 1200},
        "90d": {**w, "median": 28.5, "volume": 3500}})])
    evals = await app.market.analyze()
    ev = next(e for e in evals if e.item.key == name)
    assert ev.score.eligible and ev.score.score >= 60
    async with app.db.session() as s:
        assert (await s.execute(select(func.count(Opportunity.id)))).scalar() >= 1
    await app.alerts.on_analysis(evals)
    out = " ".join(r.text for r in await app.publisher.fetch("testnet", 30))
    assert "[SKIN OPPORTUNITY]" in out and "Printstream" in out and "Not financial advice" in out
