"""News aggregator: fetch, de-duplicate, classify, score, summarise and store."""
from __future__ import annotations

import logging
import time
from typing import Callable

from sqlalchemy import and_, desc, or_, select

from ..core.config import Config
from ..core.http import HttpClient, SourceError
from ..database.db import Database
from ..database.models import News
from ..utils.text import norm, normalize_url, sha
from ..sources.news.feeds import NewsFetcher, RawNews
from .classify import GROUPS, classify, is_cs_related, jaccard, relevance, title_tokens
from .summarize import Summarizer
from .valve import UpdateAnalysis, analyze_update

log = logging.getLogger("cs2.news")


class NewsService:
    def __init__(self, cfg: Config, db: Database, http: HttpClient) -> None:
        self.cfg = cfg
        self.db = db
        self.fetcher = NewsFetcher(cfg, http)
        self.summarizer = Summarizer(cfg, http)
        self._last_fetch: dict[str, float] = {}
        self.top_teams: Callable[[], set[str]] = lambda: set()
        self.recent: list[News] = []   # cache of the last 7 days (market context)

    @property
    def ncfg(self) -> dict:
        return self.cfg.get("settings.news", {}) or {}

    def due_feeds(self) -> list[str]:
        now = time.time()
        default = float(self.ncfg.get("interval_s", 300))
        due = []
        for name, f in self.fetcher.feeds().items():
            if not f.get("enabled", False):
                continue
            if now - self._last_fetch.get(name, 0) >= float(f.get("interval_s", default)):
                due.append(name)
        return due

    async def poll(self) -> list[News]:
        new: list[News] = []
        for name in self.due_feeds():
            self._last_fetch[name] = time.time()
            try:
                raws = await self.fetcher.fetch(name)
            except SourceError as e:
                log.info("News %s: %s", name, e)
                continue
            except Exception as e:
                log.warning("News %s: parse error %s", name, e)
                continue
            new.extend(await self.ingest(name, raws))
        if new:
            await self._summarize(new)
        await self.refresh_recent()
        return new

    async def ingest(self, source: str, raws: list[RawNews]) -> list[News]:
        f = self.fetcher.feeds().get(source, {})
        max_age = float(self.ncfg.get("max_age_h", 72)) * 3600
        weight = float(f.get("weight", 10))
        cs_filter = bool(f.get("cs_filter", False))
        now = time.time()
        out: list[News] = []
        async with self.db.session() as s:
            recent = (await s.execute(select(News.id, News.title_norm, News.source).where(
                News.published_at >= now - 72 * 3600))).all()
            recent_tokens = [(rid, set(tn.split()), src) for rid, tn, src in recent]
            for r in raws:
                if now - r.published_at > max_age:
                    continue
                if cs_filter and not is_cs_related(r.title, r.summary, r.tags):
                    continue
                url_hash = sha(normalize_url(r.url))
                exists = (await s.execute(select(News.id).where(News.url_hash == url_hash))).first()
                if exists:
                    continue
                toks = title_tokens(r.title)
                dup = next((rid for rid, tk, src in recent_tokens if src != source and jaccard(toks, tk) >= 0.6), None)
                category, tags = classify(r.title, r.summary, r.tags, r.official)
                rel = relevance(r.title, r.summary, category, weight, r.published_at, self.top_teams(), r.official)
                n = News(source=source, url=r.url, url_hash=url_hash, title=r.title, title_norm=" ".join(sorted(toks)),
                         summary=r.summary, lang=r.lang, category=category, tags=tags, relevance=rel,
                         important=rel >= float(self.ncfg.get("important_threshold", 70)), duplicate_of=dup,
                         published_at=r.published_at, fetched_at=now, body=r.body,
                         title_es=r.title if r.lang == str(self.cfg.get("settings.bot.language", "en")) else None)
                s.add(n)
                await s.flush()
                recent_tokens.append((n.id, toks, source))
                out.append(n)
        if out:
            log.info("News %s: %d new", source, len(out))
        return out

    async def _summarize(self, items: list[News]) -> None:
        todo = [n for n in items if n.duplicate_of is None]
        batch = int(self.ncfg.get("summarizer", {}).get("batch_size", 8))
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            res = await self.summarizer.summarize([{"id": n.id, "title": n.title, "summary": n.summary, "lang": n.lang}
                                                   for n in chunk])
            if not res:
                continue
            async with self.db.session() as s:
                for n in chunk:
                    sm = res.get(n.id)
                    if not sm:
                        continue
                    row = await s.get(News, n.id)
                    row.title_es, row.summary_es = sm.title_es or row.title_es, sm.summary_es
                    if sm.provider == "llm":
                        row.processed_by = "llm"
                        if sm.category and sm.category in dict.fromkeys(GROUPS["updates"] | GROUPS["market"] | GROUPS["esports"]):
                            row.category = sm.category
                        if sm.importance is not None:
                            f = self.fetcher.feeds().get(row.source, {})
                            row.relevance = relevance(row.title, row.summary, row.category, float(f.get("weight", 10)),
                                                      row.published_at, self.top_teams(), False, sm.importance)
                            row.important = row.relevance >= float(self.ncfg.get("important_threshold", 70))
                            if sm.importance == 0:
                                row.relevance = 0
                    elif sm.provider == "translate":
                        row.processed_by = "translate"
                    n.title_es, n.summary_es, n.category, n.relevance, n.important = (
                        row.title_es, row.summary_es, row.category, row.relevance, row.important)

    async def refresh_recent(self) -> None:
        async with self.db.session() as s:
            self.recent = list((await s.execute(select(News).where(and_(
                News.published_at >= time.time() - 7 * 86400, News.duplicate_of.is_(None))).order_by(
                desc(News.published_at)).limit(400))).scalars().all())

    # ----------------------------------------------------------------- queries
    async def latest(self, group: str | None = None, limit: int = 5, major: bool = False,
                     min_relevance: float = 0) -> list[News]:
        q = select(News).where(and_(News.duplicate_of.is_(None), News.relevance >= min_relevance))
        if group in GROUPS:
            q = q.where(News.category.in_(GROUPS[group]))
        if major:
            q = q.where(or_(News.title.ilike("%major%"), News.title_es.ilike("%major%")))
        async with self.db.session() as s:
            return list((await s.execute(q.order_by(desc(News.published_at)).limit(limit))).scalars().all())

    def valve_analysis(self, n: News) -> UpdateAnalysis:
        return analyze_update(n.title, n.body or n.summary)

    def related_to_item(self, ref) -> list[tuple]:
        """Recent news related to an item (for the 'news context' scoring component)."""
        p = ref.parsed
        keys = set()
        if p.skin and p.weapon:
            keys.add(norm(f"{p.weapon} {p.skin}"))
            keys.add(norm(p.skin))
        if p.item_type in ("case", "capsule", "souvenir_package"):
            keys.add(norm(p.market_hash_name))
        if ref.collection:
            keys.add(norm(ref.collection.replace("The ", "").replace(" Collection", "")))
        if p.tournament:
            keys.add(norm(p.tournament))
        keys = {k for k in keys if len(k) >= 4}
        out = []
        for n in self.recent:
            text = norm(f"{n.title} {n.summary}")
            hit = any(k in text for k in keys)
            if not hit and n.category in ("actualizacion", "parche", "operacion") and n.source in self.official_sources():
                a = self.valve_analysis(n)
                if a.market_relevant and (p.item_type in ("case", "capsule", "sticker", "souvenir_package")
                                          or (p.weapon and p.weapon in a.weapons)
                                          or any("trading" in i or "Market" in i for i in a.impacts)):
                    hit = True
            if hit:
                out.append((n.published_at, n.title_es or n.title, n.category, n.important))
        return out

    def official_sources(self) -> set[str]:
        return {k for k, v in self.fetcher.feeds().items() if v.get("official")}
