"""Application container: builds the services, registers periodic tasks and manages the lifecycle."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .alerts.engine import AlertEngine
from .alerts.publisher import Publisher
from .core.cache import TTLCache
from .core.config import ROOT, Config
from .core.health import HealthRegistry
from .core.http import HttpClient, SourceError
from .core.scheduler import Scheduler
from .database.db import Database
from .database.maintenance import backup, cleanup, rollup_snapshots
from .database.models import SystemLog, Watch
from .esports.service import EsportsService
from .irc.commands import CommandRouter
from .irc.format import Formatter
from .market.service import MarketService
from .news.service import NewsService

log = logging.getLogger("cs2.app")


def _abs(p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


class App:
    def __init__(self, cfg: Config, db_url: str | None = None, http_transport=None) -> None:
        self.cfg = cfg
        data_dir = _abs(cfg.get("settings.paths.data", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = _abs(cfg.get("settings.paths.backups", "backups"))
        url = db_url or os.environ.get("DATABASE_URL") or f"sqlite+aiosqlite:///{(data_dir / 'cs2intel.db').as_posix()}"
        self.db = Database(url)
        self.health = HealthRegistry()
        self.cache = TTLCache(redis_url=os.environ.get("REDIS_URL") or None)
        self.http = HttpClient(cfg, self.health, self.cache, transport=http_transport)
        self.scheduler = Scheduler()
        self.fmt = Formatter(colors=bool(cfg.get("settings.irc.colors", True)),
                             tz=cfg.get("settings.bot.timezone", "Europe/Madrid"),
                             disclaimer=cfg.get("settings.market.disclaimer", "Not financial advice."))
        self.publisher = Publisher(cfg, self.db)
        self.news = NewsService(cfg, self.db, self.http)
        self.market = MarketService(cfg, self.db, self.http)
        self.esports = EsportsService(cfg, self.db, self.http)
        self.alerts = AlertEngine(cfg, self.db, self.publisher, self.fmt, self.news, self.market, self.esports)
        self.commands = CommandRouter(self)
        self.market.news_lookup = self.news.related_to_item
        self.news.top_teams = lambda: self.esports.top_team_norms
        self.esports.on_events = self.alerts.on_match_events
        self.heartbeats: dict[str, dict] = {}
        self.started = time.time()
        self.health.on_incident(self._incident)
        self._pending_incidents: list[tuple] = []

    def _incident(self, source: str, old: str, new: str) -> None:
        if new in ("blocked", "down") or (old in ("blocked", "down") and new == "ok"):
            self._pending_incidents.append((time.time(), "WARNING" if new != "ok" else "INFO", source, f"{old} → {new}"))

    def chart_url(self, item_id: int) -> str | None:
        base = (self.cfg.get("settings.api.public_url") or "").rstrip("/")
        if not base or not self.cfg.get("settings.api.public_charts", False):
            return None
        return f"{base}/charts/{item_id}.svg"

    # ------------------------------------------------------------ startup
    async def start(self, run_scheduler: bool = True) -> None:
        await self.db.init()
        await self.publisher.load()
        await self.market.load()
        await self.esports.load()
        for name in ("steam", "csfloat", "buff163", "pricempire"):
            a = self.market.adapters[name]
            if not a.conf.get("enabled"):
                self.health.disabled(name, "disabled in sources.yaml")
            elif not a.configured:
                self.health.disabled(name, "missing credential")
        if run_scheduler:
            self._register_tasks()
            self.scheduler.start()
        log.info("CS2 Intelligence backend started")

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.http.close()
        await self.db.close()

    # ---------------------------------------------------------------- tasks
    def _iv(self, key: str, default: float) -> float:
        return float(self.cfg.get(f"settings.intervals.{key}", default))

    def _register_tasks(self) -> None:
        sch, cfg = self.scheduler, self.cfg
        sch.add("fx", self.market.refresh_fx, lambda: self._iv("fx_s", 6 * 3600))
        sch.add("news", self.task_news, lambda: self._iv("news_check_s", 60), initial_delay=5)
        if cfg.source_enabled("skinport"):
            sch.add("skinport_items", lambda: self.market.poll_bulk("skinport"),
                    lambda: max(300.0, self._iv("skinport_items_s", 600)), initial_delay=10)
            sch.add("skinport_history", lambda: self.market.poll_stats("skinport"),
                    lambda: max(600.0, self._iv("skinport_history_s", 3600)), initial_delay=40)
        if cfg.source_enabled("steam"):
            sch.add("steam_queue", self.task_steam, lambda: self._iv("steam_queue_s", 120), initial_delay=60)
            if cfg.source("steam").get("bulk_pages", 0):
                sch.add("steam_bulk", lambda: self.market.poll_bulk("steam"), lambda: self._iv("steam_bulk_s", 3600),
                        initial_delay=300)
        if self.market.adapters["csfloat"].enabled:
            sch.add("csfloat", self.task_csfloat, lambda: self._iv("csfloat_s", 900), initial_delay=90)
        if self.market.adapters["buff163"].enabled:
            sch.add("buff163", lambda: self.market.poll_bulk("buff163"), lambda: self._iv("buff_s", 1800), initial_delay=120)
        if self.market.adapters["pricempire"].enabled:
            sch.add("pricempire", lambda: self.market.poll_bulk("pricempire"), lambda: self._iv("pricempire_s", 3600),
                    initial_delay=150)
        if cfg.source_enabled("metadata"):
            sch.add("metadata", self.market.load_metadata, lambda: self._iv("metadata_s", 7 * 86400), initial_delay=180)
        sch.add("market_analysis", self.task_analysis, lambda: self._iv("analysis_s", 900), initial_delay=240)
        sch.add("watchlists", self.alerts.check_watchlists, lambda: self._iv("watchlist_s", 300), initial_delay=200)
        sch.add("trend_digest", self.alerts.trend_digest, lambda: self._iv("trend_digest_s", 6 * 3600), initial_delay=1800)
        # esports: fast intervals while matches are live
        for src in ("hltv", "pandascore", "faceit", "liquipedia"):
            if self.esports.source_ready(src):
                sch.add(f"live_{src}", (lambda s=src: self.esports.poll(s)), (lambda s=src: self.esports.live_interval(s)),
                        initial_delay=15, jitter=0.05)
        for src in ("hltv", "pandascore"):
            if self.esports.source_ready(src):
                sch.add(f"results_{src}", (lambda s=src: self.esports.poll(s, results=True)),
                        lambda: self._iv("results_s", 60), initial_delay=45)
        sch.add("upcoming_reminders", self.alerts.upcoming_reminders, 60, initial_delay=120)
        if self.esports.source_ready("liquipedia"):
            sch.add("tournaments", self.esports.refresh_tournaments, lambda: self._iv("tournaments_s", 6 * 3600),
                    initial_delay=70)
        if self.esports.source_ready("vrs"):
            sch.add("ranking", self.esports.refresh_ranking, lambda: self._iv("ranking_s", 12 * 3600), initial_delay=20)
        sch.add("maintenance", self.task_maintenance, lambda: self._iv("maintenance_s", 86400), initial_delay=3600)
        sch.add("backup", self.task_backup, lambda: self._iv("backup_s", 86400), initial_delay=7200)
        sch.add("incidents", self.task_incidents, 30, initial_delay=30)

    async def task_news(self) -> None:
        new = await self.news.poll()
        if new:
            await self.alerts.on_news(new)

    async def task_steam(self) -> None:
        self.market.queue_steam(await self.watched_keys(), priority=3)
        self.market.queue_steam(list(self.cfg.source("steam").get("tracked_items") or []), priority=4)
        await self.market.drain_steam_queue()

    async def task_csfloat(self) -> None:
        keys = await self.watched_keys()
        top = [e.item.key for e in sorted(self.market.last_evaluations, key=lambda e: -e.score.score)[:10]]
        keys += list(self.cfg.source("csfloat").get("tracked_items") or [])
        await self.market.refresh_item_sources(list(dict.fromkeys(keys + top)))

    async def task_analysis(self) -> None:
        evals = await self.market.analyze()
        await self.alerts.on_analysis(evals)

    async def task_maintenance(self) -> None:
        await rollup_snapshots(self.db, int(self.cfg.get("settings.maintenance.raw_snapshot_days", 7)))
        await cleanup(self.db, self.cfg.get("settings.maintenance.retention", {}) or {})

    async def task_backup(self) -> None:
        await backup(self.db, self.backup_dir, int(self.cfg.get("settings.maintenance.backup_keep", 14)))

    async def task_incidents(self) -> None:
        items, self._pending_incidents = self._pending_incidents, []
        if not items:
            return
        async with self.db.session() as s:
            for ts, lvl, comp, msg in items:
                s.add(SystemLog(ts=ts, level=lvl, component=comp, message=f"source {comp}: {msg}"))

    async def watched_keys(self) -> list[str]:
        from sqlalchemy import select
        async with self.db.session() as s:
            ids = {r for (r,) in (await s.execute(select(Watch.item_id))).all()}
        return [self.market.by_id[i].key for i in ids if i in self.market.by_id]

    # --------------------------------------------------------------- health
    async def health_report(self) -> dict:
        db_ok = await self.db.ping()
        tasks = [t.as_dict() for t in self.scheduler.tasks.values()]
        failing = [t["name"] for t in tasks if t["last_error"]]
        return {"status": "ok" if db_ok else "error", "db": db_ok, "uptime_s": int(time.time() - self.started),
                "paused": self.publisher.paused, "sources": [h.as_dict() for h in self.health.all()],
                "tasks": tasks, "failing_tasks": failing, "eggdrops": self.heartbeats,
                "live_matches": len(self.esports.live())}
