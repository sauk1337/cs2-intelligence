"""Market service: ingestion, price history, search and opportunity analysis."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from rapidfuzz import fuzz, process
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..analytics.arbitrage import compute_arbitrage
from ..analytics.metrics import DAY, ItemData, Metrics, compute_metrics
from ..analytics.scoring import ScoreResult, score_drop
from ..core.config import Config
from ..core.http import HttpClient, SourceError
from ..database.db import Database
from ..database.models import Item, MarketLatest, MarketStat, Opportunity, PriceSnapshot
from ..sources.market.base import Fees, MarketAdapter, Quote, SalesStats
from ..sources.market.buff import Buff163
from ..sources.market.csfloat import CSFloat
from ..sources.market.fx import FxRates
from ..sources.market.pricempire import Pricempire
from ..sources.market.skinport import Skinport
from ..sources.market.steam import SteamMarket
from ..utils.text import norm
from .items import ParsedItem, parse_market_name, parse_query, search_key

log = logging.getLogger("cs2.market")
METADATA_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"


@dataclass
class ItemRef:
    id: int
    key: str
    parsed: ParsedItem
    collection: str | None = None
    rarity: str | None = None


@dataclass
class Evaluation:
    item: ItemRef
    metrics: Metrics
    score: ScoreResult
    arbitrage: list


class MarketService:
    def __init__(self, cfg: Config, db: Database, http: HttpClient) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.base_currency = str(cfg.get("settings.market.currency", "EUR")).upper()
        self.fx = FxRates(http, self.base_currency)
        self.adapters: dict[str, MarketAdapter] = {a.name: a for a in (
            Skinport(cfg, http), SteamMarket(cfg, http), CSFloat(cfg, http), Buff163(cfg, http), Pricempire(cfg, http))}
        self.items: dict[str, ItemRef] = {}
        self.by_id: dict[int, ItemRef] = {}
        self._names: list[str] = []
        self._names_ids: list[int] = []
        self.bulk_stats: dict[int, dict] = {}          # item_id -> {window: {...}} (reference source)
        self.latest: dict[int, dict[str, dict]] = {}   # item_id -> source -> {...}
        self._last_snapshot: dict[str, float] = {}
        self.steam_queue: dict[str, float] = {}        # market_hash_name -> priority (lower = sooner)
        self.news_lookup: Callable[[ItemRef], list] = lambda item: []
        self.last_analysis: float | None = None
        self.last_evaluations: list[Evaluation] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- config
    @property
    def mcfg(self) -> dict:
        return self.cfg.get("settings.market", {}) or {}

    @property
    def reference_source(self) -> str:
        return str(self.mcfg.get("reference_source", "skinport"))

    def fees(self) -> dict[str, Fees]:
        return {name: a.fees() for name, a in self.adapters.items()}

    def enabled_adapters(self) -> list[MarketAdapter]:
        return [a for a in self.adapters.values() if a.enabled]

    # ------------------------------------------------------------ startup
    async def load(self) -> None:
        async with self.db.session() as s:
            rows = (await s.execute(select(Item))).scalars().all()
            for it in rows:
                self._register(it)
            latest = (await s.execute(select(MarketLatest))).scalars().all()
            for r in latest:
                self.latest.setdefault(r.item_id, {})[r.source] = self._latest_dict(r)
            stats = (await s.execute(select(MarketStat).where(MarketStat.source == self.reference_source))).scalars().all()
            for r in stats:
                self.bulk_stats.setdefault(r.item_id, {})[r.window] = {
                    "min": r.min, "max": r.max, "avg": r.avg, "median": r.median, "volume": r.volume}
        self._rebuild_names()
        log.info("Market: %d items, %d with price, %d with statistics", len(self.items), len(self.latest),
                 len(self.bulk_stats))

    @staticmethod
    def _latest_dict(r: MarketLatest) -> dict:
        return {"price": r.price, "median": r.median, "bid": r.bid, "volume_24h": r.volume_24h, "listings": r.listings,
                "ts": r.ts, "url": r.url, "currency": r.currency}

    def _register(self, it: Item) -> ItemRef:
        mhn = it.market_hash_name
        if it.phase and mhn.endswith(f" [{it.phase}]"):
            mhn = mhn[: -len(f" [{it.phase}]")]  # the unique key stores the phase as a suffix
        parsed = ParsedItem(market_hash_name=mhn, base_name=it.base_name, item_type=it.item_type,
                            weapon=it.weapon, skin=it.skin, exterior=it.exterior, stattrak=it.stattrak,
                            souvenir=it.souvenir, phase=it.phase, tournament=it.tournament)
        ref = ItemRef(id=it.id, key=parsed.key, parsed=parsed, collection=it.collection, rarity=it.rarity)
        self.items[ref.key] = ref
        self.by_id[ref.id] = ref
        return ref

    def _rebuild_names(self) -> None:
        self._names = [search_key(r.key) for r in self.items.values()]
        self._names_ids = [r.id for r in self.items.values()]

    async def ensure_items(self, names: list[tuple[str, str | None]]) -> dict[str, ItemRef]:
        """Create (if missing) the items for [(market_hash_name, version)]. Returns key → ItemRef."""
        missing = []
        for name, version in names:
            p = parse_market_name(name, version)
            if p.key not in self.items:
                missing.append(p)
        if missing:
            async with self.db.session() as s:
                t = time.time()
                for p in {p.key: p for p in missing}.values():
                    s.add(Item(market_hash_name=p.key, base_name=p.base_name, search_key=search_key(p.key),
                               item_type=p.item_type, weapon=p.weapon, skin=p.skin, exterior=p.exterior,
                               stattrak=p.stattrak, souvenir=p.souvenir, phase=p.phase, tournament=p.tournament,
                               created_at=t))
                await s.flush()
                rows = (await s.execute(select(Item).where(Item.market_hash_name.in_([p.key for p in missing])))).scalars().all()
                for it in rows:
                    self._register(it)
            self._rebuild_names()
        out = {}
        for name, version in names:
            key = parse_market_name(name, version).key
            if key in self.items:
                out[key] = self.items[key]
        return out

    # ---------------------------------------------------------------- upsert
    def _insert(self, model):
        return sqlite_insert(model) if self.db.is_sqlite else pg_insert(model)

    async def ingest_quotes(self, quotes: list[Quote], snapshot: bool | None = None) -> int:
        """Convert to the base currency, store the latest price and a history snapshot."""
        if not quotes:
            return 0
        min_price = float(self.mcfg.get("min_price_track", 0.3))
        refs = await self.ensure_items([(q.market_hash_name, q.version) for q in quotes])
        t = time.time()
        source = quotes[0].source
        if snapshot is None:
            every = float(self.mcfg.get("snapshot_every_s", 3600))
            snapshot = t - self._last_snapshot.get(source, 0) >= every
        latest_rows, snap_rows = [], []
        for q in quotes:
            ref = refs.get(parse_market_name(q.market_hash_name, q.version).key)
            if not ref:
                continue
            price = self.fx.convert(q.price, q.currency)
            if q.price is not None and price is None:
                continue  # no exchange rate: never store a wrong price
            median, bid = self.fx.convert(q.median, q.currency), self.fx.convert(q.bid, q.currency)
            row = {"item_id": ref.id, "source": q.source, "ts": t, "currency": self.base_currency, "price": price,
                   "median": median, "bid": bid, "volume_24h": q.volume_24h, "listings": q.listings, "url": q.url}
            latest_rows.append(row)
            self.latest.setdefault(ref.id, {})[q.source] = {**row, "ts": t}
            if snapshot and price is not None and price >= min_price:
                snap_rows.append({"item_id": ref.id, "source": q.source, "ts": t, "currency": self.base_currency,
                                  "price": price, "median": median, "bid": bid, "volume_24h": q.volume_24h,
                                  "listings": q.listings, "raw_currency": q.currency, "raw_price": q.price,
                                  "granularity": "raw"})
        # one transaction per chunk: releases SQLite's write lock between chunks
        for i in range(0, len(latest_rows), 500):
            chunk = latest_rows[i:i + 500]
            async with self.db.session() as s:
                stmt = self._insert(MarketLatest).values(chunk)
                stmt = stmt.on_conflict_do_update(index_elements=["item_id", "source"], set_={
                    c: getattr(stmt.excluded, c) for c in ("ts", "currency", "price", "median", "bid", "volume_24h",
                                                           "listings", "url")})
                await s.execute(stmt)
        for i in range(0, len(snap_rows), 1000):
            async with self.db.session() as s:
                await s.execute(PriceSnapshot.__table__.insert(), snap_rows[i:i + 1000])
        if snapshot:
            self._last_snapshot[source] = t
        log.info("Ingest %s: %d prices, %d snapshots", source, len(latest_rows), len(snap_rows))
        return len(latest_rows)

    async def ingest_stats(self, stats: list[SalesStats]) -> int:
        if not stats:
            return 0
        refs = await self.ensure_items([(st.market_hash_name, st.version) for st in stats])
        t = time.time()
        rows = []
        for st in stats:
            ref = refs.get(parse_market_name(st.market_hash_name, st.version).key)
            if not ref:
                continue
            conv = {}
            for w, v in st.windows.items():
                c = {k: (self.fx.convert(v.get(k), st.currency) if k != "volume" else v.get("volume"))
                     for k in ("min", "max", "avg", "median", "volume")}
                conv[w] = c
                if c["median"] is None and c["min"] is None and not c["volume"]:
                    continue  # window without sales: no row stored
                rows.append({"item_id": ref.id, "source": st.source, "window": w, "ts": t,
                             "currency": self.base_currency, **c})
            if st.source == self.reference_source:
                self.bulk_stats[ref.id] = conv
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            async with self.db.session() as s:
                stmt = self._insert(MarketStat).values(chunk)
                stmt = stmt.on_conflict_do_update(index_elements=["item_id", "source", "window"], set_={
                    c: getattr(stmt.excluded, c) for c in ("ts", "currency", "min", "max", "avg", "median", "volume")})
                await s.execute(stmt)
        log.info("Sales statistics %s: %d items", stats[0].source, len(stats))
        return len(stats)

    # ------------------------------------------------------- source tasks
    async def refresh_fx(self) -> None:
        await self.fx.refresh()

    async def poll_bulk(self, source: str) -> int:
        a = self.adapters[source]
        if not a.enabled:
            return 0
        return await self.ingest_quotes(await a.fetch_bulk())

    async def poll_stats(self, source: str = "skinport") -> int:
        a = self.adapters[source]
        if not a.enabled:
            return 0
        return await self.ingest_stats(await a.fetch_stats_bulk())

    def queue_steam(self, names: list[str], priority: float = 5.0) -> None:
        for n in names:
            self.steam_queue[n] = min(priority, self.steam_queue.get(n, 99))

    async def drain_steam_queue(self, max_items: int | None = None) -> int:
        a = self.adapters["steam"]
        if not a.enabled or not self.steam_queue:
            return 0
        max_items = max_items or int(self.cfg.source("steam").get("max_items_per_run", 15))
        batch = sorted(self.steam_queue, key=lambda n: self.steam_queue[n])[:max_items]
        quotes = []
        for name in batch:
            self.steam_queue.pop(name, None)
            try:
                q = await a.fetch_item(name.split(" [")[0])
            except SourceError as e:
                log.info("Steam queue stopped: %s", e)
                break
            if q:
                quotes.append(q)
        await self.ingest_quotes(quotes, snapshot=True)
        return len(quotes)

    async def refresh_item_sources(self, names: list[str]) -> int:
        """Per-item price lookups (CSFloat) for watched items and candidates."""
        a = self.adapters["csfloat"]
        if not a.enabled:
            return 0
        quotes = []
        for key in names[: int(self.cfg.source("csfloat").get("max_items_per_run", 20))]:
            ref = self.items.get(key)
            try:
                q = await a.fetch_item(key.split(" [")[0], ref.parsed.phase if ref else None)
            except SourceError as e:
                log.info("CSFloat stopped: %s", e)
                break
            if q:
                quotes.append(q)
        await self.ingest_quotes(quotes, snapshot=True)
        return len(quotes)

    async def load_metadata(self) -> int:
        """Collection and rarity from the public ByMykel/CSGO-API dataset (static, no prices)."""
        if not self.cfg.source_enabled("metadata"):
            return 0
        data = await self.http.get_json("metadata", self.cfg.source("metadata").get("url", METADATA_URL))
        meta = {}
        for s in data or []:
            name = (s.get("name") or "").replace("★ ", "").strip()
            cols = s.get("collections") or []
            meta[norm(name)] = ((cols[0]["name"] if cols else None), (s.get("rarity") or {}).get("name"))
        updated = 0
        async with self.db.session() as sess:
            for ref in self.items.values():
                if ref.parsed.item_type not in ("weapon", "knife", "gloves"):
                    continue
                m = meta.get(norm(ref.parsed.base_name.replace("★ ", "")))
                if m and (m[0] != ref.collection or m[1] != ref.rarity):
                    it = await sess.get(Item, ref.id)
                    if it:
                        it.collection, it.rarity = m
                        ref.collection, ref.rarity = m
                        updated += 1
        log.info("Metadata updated for %d items", updated)
        return updated

    # ------------------------------------------------------------------ search
    def resolve(self, text: str, limit: int = 5) -> list[ItemRef]:
        """Tolerant autocompletion: 'ak redline ft', 'st awp asiimov', 'karambit doppler'."""
        text = text.strip()
        if not text or not self.items:
            return []
        exact = self.items.get(text)
        if exact:
            return [exact]
        q = parse_query(text)
        if not q.terms:
            return []
        cands = []
        for ref in self.items.values():
            key = search_key(ref.key)
            if all(t in key.split() or t in key for t in q.terms):
                cands.append(ref)
        if not cands:
            qs = " ".join(q.terms)
            hits = process.extract(qs, self._names, scorer=fuzz.WRatio, limit=40, score_cutoff=70)
            cands = [self.by_id[self._names_ids[i]] for _, _, i in hits]

        def rank(ref: ItemRef) -> tuple:
            p = ref.parsed
            st_pen = 0 if (q.stattrak or False) == p.stattrak else 1
            sv_pen = 0 if (q.souvenir or False) == p.souvenir else 1
            ext_pen = 0 if (q.exterior is None or q.exterior == p.exterior) else 2
            extra = len(search_key(p.base_name).split()) - len(q.terms)
            lat = self.latest.get(ref.id, {})
            vol = (self.bulk_stats.get(ref.id, {}).get("7d") or {}).get("volume") or 0
            has_price = 0 if any(v.get("price") for v in lat.values()) else 1
            return (ext_pen + st_pen + sv_pen, has_price, max(0, extra), -vol, len(ref.key))

        cands.sort(key=rank)
        if q.exterior:
            cands = [c for c in cands if c.parsed.exterior == q.exterior] or cands
        return cands[:limit]

    def variants(self, ref: ItemRef) -> list[ItemRef]:
        return sorted([r for r in self.items.values() if r.parsed.base_name == ref.parsed.base_name
                       and r.parsed.stattrak == ref.parsed.stattrak and r.parsed.souvenir == ref.parsed.souvenir],
                      key=lambda r: (r.parsed.exterior or "", r.parsed.phase or ""))

    # -------------------------------------------------------------- evaluation
    async def series(self, item_id: int, days: int = 90, source: str | None = None) -> dict[str, list]:
        since = time.time() - days * DAY
        q = select(PriceSnapshot.source, PriceSnapshot.ts, PriceSnapshot.price).where(
            and_(PriceSnapshot.item_id == item_id, PriceSnapshot.ts >= since)).order_by(PriceSnapshot.ts)
        if source:
            q = q.where(PriceSnapshot.source == source)
        out: dict[str, list] = {}
        async with self.db.session() as s:
            for src, ts, price in (await s.execute(q)).all():
                out.setdefault(src, []).append((ts, price))
        return out

    async def item_data(self, ref: ItemRef) -> ItemData:
        stats = {}
        async with self.db.session() as s:
            rows = (await s.execute(select(MarketStat).where(MarketStat.item_id == ref.id))).scalars().all()
            for r in rows:
                stats.setdefault(r.source, {})[r.window] = {"min": r.min, "max": r.max, "avg": r.avg,
                                                            "median": r.median, "volume": r.volume}
        return ItemData(name=ref.key, short=ref.parsed.short, item_type=ref.parsed.item_type,
                        currency=self.base_currency, reference_source=self.reference_source,
                        latest=dict(self.latest.get(ref.id, {})), stats=stats, series=await self.series(ref.id),
                        news=self.news_lookup(ref), collection=ref.collection, rarity=ref.rarity)

    async def evaluate(self, ref: ItemRef) -> Evaluation:
        d = await self.item_data(ref)
        m = compute_metrics(d, self.mcfg)
        fee = self.adapters.get(m.source).fees().seller_fee if m.source in self.adapters else None
        sc = score_drop(m, self.cfg.scoring, d.news, seller_fee=fee)
        arb = compute_arbitrage(m, d.latest, self.fees(), self.cfg.get("scoring.arbitrage", {}))
        return Evaluation(item=ref, metrics=m, score=sc, arbitrage=arb)

    # ----------------------------------------------------------- bulk analysis
    def _quick_rows(self) -> list[dict]:
        """Quick indicators for every item with reference data (no DB queries)."""
        min_price = float(self.mcfg.get("min_price_analyze", 1.0))
        min_vol = int(self.mcfg.get("min_sales_30d", 10))
        out = []
        for item_id, st in self.bulk_stats.items():
            ref = self.by_id.get(item_id)
            lat = (self.latest.get(item_id) or {}).get(self.reference_source) or {}
            price = lat.get("price")
            if not ref or price is None or price < min_price:
                continue
            w24, w7, w30, w90 = (st.get(k) or {} for k in ("24h", "7d", "30d", "90d"))
            if (w30.get("volume") or 0) < min_vol or not w30.get("median"):
                continue
            row = {"ref": ref, "price": price, "listings": lat.get("listings"), "url": lat.get("url"),
                   "med7": w7.get("median"), "med30": w30.get("median"), "med90": w90.get("median"),
                   "min90": w90.get("min"), "vol24": w24.get("volume") or 0, "vol7": w7.get("volume") or 0,
                   "vol30": w30.get("volume") or 0}
            row["vs7"] = (price - row["med7"]) / row["med7"] * 100 if row["med7"] else None
            row["vs30"] = (price - row["med30"]) / row["med30"] * 100
            row["t7_30"] = (row["med7"] - row["med30"]) / row["med30"] * 100 if row["med7"] else None
            row["t30_90"] = (row["med30"] - row["med90"]) / row["med90"] * 100 if row["med90"] else None
            row["vol_ratio"] = row["vol24"] / (row["vol7"] / 7) if row["vol7"] else None
            out.append(row)
        return out

    async def analyze(self) -> list[Evaluation]:
        """Detect drops → evaluate candidates with the full engine → store opportunities."""
        async with self._lock:
            rows = self._quick_rows()
            th = {**{"below_avg30": 8.0, "drop_7d": 10.0}, **(self.cfg.get("scoring.thresholds", {}) or {})}
            cands = [r for r in rows if (r["vs30"] <= -th["below_avg30"]) or (r["vs7"] is not None and r["vs7"] <= -th["drop_7d"] / 2)]
            cands.sort(key=lambda r: (r["vs30"], -r["vol30"]))
            max_eval = int(self.mcfg.get("max_candidates", 150))
            evals: list[Evaluation] = []
            for r in cands[:max_eval]:
                try:
                    evals.append(await self.evaluate(r["ref"]))
                except Exception as e:  # one bad item never breaks the analysis
                    log.debug("evaluate %s: %s", r["ref"].key, e)
            # items priced on ≥2 platforms may be arbitrage even without a drop
            multi = [iid for iid, srcs in self.latest.items() if len([s for s in srcs.values() if s.get("price")]) >= 2]
            seen = {e.item.id for e in evals}
            for iid in multi[: int(self.mcfg.get("max_arbitrage_checks", 300))]:
                if iid not in seen and iid in self.by_id:
                    try:
                        ev = await self.evaluate(self.by_id[iid])
                        if ev.arbitrage:
                            evals.append(ev)
                    except Exception as e:
                        log.debug("arb %s: %s", iid, e)
            # ask other platforms to confirm the best candidates
            top = sorted([e for e in evals if e.score.eligible], key=lambda e: -e.score.score)[:20]
            self.queue_steam([e.item.key for e in top], priority=1)
            self.last_evaluations = evals
            self.last_analysis = time.time()
            await self._store(evals)
            log.info("Analysis: %d items, %d candidates, %d evaluated", len(rows), len(cands), len(evals))
            return evals

    async def _store(self, evals: list[Evaluation]) -> None:
        min_score = float(self.cfg.get("scoring.store_min_score", 50))
        t = time.time()
        async with self.db.session() as s:
            for e in evals:
                m = e.metrics
                if e.score.eligible and e.score.score >= min_score:
                    s.add(Opportunity(item_id=e.item.id, ts=t, kind="drop", label=e.score.label, score=e.score.score,
                                      risk=e.score.risk, source=m.source, price=m.price,
                                      reference_price=m.median30, change_pct=(m.change.get("24h") or (None,))[0],
                                      currency=m.currency, details={**e.score.as_dict(), "metrics": _metrics_brief(m)},
                                      url=m.url))
                for a in e.arbitrage[:1]:
                    if a.score >= min_score:
                        s.add(Opportunity(item_id=e.item.id, ts=t, kind="arbitrage", label=a.label, score=a.score,
                                          risk="HIGH" if a.label == "HIGH RISK" else "MEDIUM", source=a.buy_source,
                                          price=a.buy_price, reference_price=a.sell_price, change_pct=a.profit_pct,
                                          currency=m.currency, details=a.as_dict(), url=a.buy_url))

    # ------------------------------------------------------------------ lists
    def top_lists(self, kind: str, limit: int = 8) -> list[dict]:
        rows = self._quick_rows()
        if kind == "gainers":
            rows = [r for r in rows if r["t7_30"] is not None]
            rows.sort(key=lambda r: -r["t7_30"])
        elif kind == "losers":
            rows = [r for r in rows if r["t7_30"] is not None]
            rows.sort(key=lambda r: r["t7_30"])
        elif kind == "drops":
            rows.sort(key=lambda r: r["vs30"])
        elif kind == "volume":
            rows = [r for r in rows if r["vol_ratio"] is not None and r["vol7"] >= 14]
            rows.sort(key=lambda r: -r["vol_ratio"])
        elif kind == "top":
            rows.sort(key=lambda r: -(r["vol7"] * r["price"]))  # estimated traded volume
        elif kind == "trending":
            rows = [r for r in rows if r["t7_30"] is not None and r["vol_ratio"] is not None]
            rows.sort(key=lambda r: -(abs(r["t7_30"]) * min(3, r["vol_ratio"])))
        elif kind == "lowliq":
            rows.sort(key=lambda r: r["vol30"])
        return rows[:limit]

    def by_type(self, types: tuple[str, ...], limit: int = 8, tournament_only: bool = False) -> list[dict]:
        rows = [r for r in self._quick_rows() if r["ref"].parsed.item_type in types
                and (not tournament_only or r["ref"].parsed.tournament)]
        rows.sort(key=lambda r: -(abs(r["t7_30"] or 0) * (r["vol7"] or 0) ** 0.5))
        return rows[:limit]

    def collections(self, limit: int = 8) -> list[dict]:
        agg: dict[str, dict] = {}
        for r in self._quick_rows():
            col = r["ref"].collection
            if not col or r["t7_30"] is None:
                continue
            a = agg.setdefault(col, {"name": col, "n": 0, "trend_sum": 0.0, "vol7": 0})
            a["n"] += 1
            a["trend_sum"] += r["t7_30"]
            a["vol7"] += r["vol7"]
        rows = [{**a, "trend": a["trend_sum"] / a["n"]} for a in agg.values() if a["n"] >= 3]
        rows.sort(key=lambda a: -a["trend"])
        return rows[:limit]

    def market_stats(self) -> dict:
        rows = self._quick_rows()
        t = [r["t7_30"] for r in rows if r["t7_30"] is not None]
        up = len([x for x in t if x > 2])
        down = len([x for x in t if x < -2])
        t.sort()
        return {"items": len(self.items), "with_price": len(self.latest), "analyzed": len(rows),
                "median_trend": t[len(t) // 2] if t else None, "up": up, "down": down,
                "volume7d": sum(r["vol7"] for r in rows),
                "sources": {n: a.enabled for n, a in self.adapters.items()}, "last_analysis": self.last_analysis,
                "fx_date": self.fx.date}

    async def recent_opportunities(self, kind: str = "drop", hours: float = 24, limit: int = 6) -> list[tuple]:
        since = time.time() - hours * 3600
        async with self.db.session() as s:
            sub = (select(Opportunity.item_id, func.max(Opportunity.id).label("mid"))
                   .where(and_(Opportunity.kind == kind, Opportunity.ts >= since)).group_by(Opportunity.item_id)
                   .subquery())
            rows = (await s.execute(select(Opportunity).join(sub, Opportunity.id == sub.c.mid)
                                    .order_by(Opportunity.score.desc()).limit(limit))).scalars().all()
        return [(self.by_id.get(o.item_id), o) for o in rows if o.item_id in self.by_id]


def _metrics_brief(m: Metrics) -> dict:
    return {"price": m.price, "median30": m.median30, "min90": m.min90, "max90": m.max90,
            "change": {k: round(v[0], 2) for k, v in m.change.items()}, "volume_7d": m.volume_7d,
            "liquidity": round(m.liquidity, 2), "manipulation": round(m.manipulation, 2),
            "trend": m.trend_label, "cross_diff_pct": m.cross_diff_pct}
