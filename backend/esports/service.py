"""Esports intelligence: multi-source LIVE matches, results, tournaments, teams, players, ranking.

LIVE reconciliation
-------------------
Every source contributes match "observations". They are grouped per fixture (normalised team
pair + nearby start time). For each fixture the most reliable and freshest observation is the
primary one (configurable priority + freshness). If two fresh sources disagree on the series
score it is flagged `conflict` and the incident is logged. If no source has confirmed the match
recently it is flagged `stale` and never presented as current.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import and_, desc, or_, select

from ..core.config import Config
from ..core.http import HttpClient, SourceError
from ..database.db import Database
from ..database.models import Match, MatchFollow, Player, Ranking, Team, Tournament
from ..sources.esports.apis import Faceit, PandaScore, ValveStandings
from ..sources.esports.base import PLAYOFF_STAGE, LiveMatch
from ..sources.esports.hltv import Hltv
from ..sources.esports.liquipedia import Liquipedia
from ..utils.text import norm, team_norm

log = logging.getLogger("cs2.esports")


@dataclass
class MatchEvent:
    kind: str            # match_start | map_change | map_end | match_end | score
    match: dict
    detail: str = ""
    importance: float = 0.0
    map_name: str | None = None
    teams_norm: tuple = field(default_factory=tuple)


class EsportsService:
    def __init__(self, cfg: Config, db: Database, http: HttpClient) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.liquipedia = Liquipedia(http)
        self.hltv = Hltv(http, int(cfg.source("hltv").get("detail_limit", 6)))
        pc = cfg.source("pandascore")
        self.pandascore = PandaScore(http, pc.get("token", ""), pc.get("game", "csgo"))
        fc = cfg.source("faceit")
        self.faceit = Faceit(http, fc.get("api_key", ""), fc.get("championships") or [], fc.get("hubs") or [])
        self.vrs = ValveStandings(http, cfg.source("vrs").get("region", "global"))
        self.obs: dict[str, dict[str, LiveMatch]] = {}       # source -> source_id -> observation
        self.obs_ts: dict[str, float] = {}                    # last successful poll per source
        self.matches: dict[str, dict] = {}                   # canonical key -> state
        self.on_events: Callable[[list[MatchEvent]], Awaitable[None]] | None = None
        self._conflicts_logged: set[str] = set()
        self.top_team_norms: set[str] = set()
        self.ranking_edition: str | None = None

    # ---------------------------------------------------------------- config
    @property
    def ecfg(self) -> dict:
        return self.cfg.get("settings.esports", {}) or {}

    def source_ready(self, name: str) -> bool:
        c = self.cfg.source(name)
        if not c.get("enabled"):
            return False
        if name == "pandascore":
            return bool(c.get("token"))
        if name == "faceit":
            return bool(c.get("api_key")) and bool((c.get("championships") or []) + (c.get("hubs") or []))
        return True

    def priority(self) -> list[str]:
        return list(self.ecfg.get("live_priority", ["hltv", "pandascore", "faceit", "liquipedia"]))

    def base_confidence(self, src: str) -> float:
        return float((self.ecfg.get("source_confidence") or {}).get(src, {"hltv": 0.95, "pandascore": 0.9,
                                                                         "faceit": 0.9, "liquipedia": 0.75}.get(src, 0.6)))

    def stale_after(self, src: str) -> float:
        return float((self.ecfg.get("stale_after_s") or {}).get(src, 180))

    def has_live(self) -> bool:
        return any(m["status"] == "live" for m in self.matches.values())

    def live_interval(self, src: str) -> float:
        iv = self.cfg.source(src).get("interval_s") or {}
        if isinstance(iv, (int, float)):
            return float(iv)
        return float(iv.get("live", 15) if self.has_live() else iv.get("idle", 120))

    # ---------------------------------------------------------------- load
    async def load(self) -> None:
        since = time.time() - 2 * 86400
        async with self.db.session() as s:
            rows = (await s.execute(select(Match).where(or_(Match.status != "finished", Match.updated_at >= since)))).scalars().all()
            for r in rows:
                self.matches[r.key] = self._row_dict(r)
            rk = (await s.execute(select(Ranking).order_by(desc(Ranking.edition), Ranking.position).limit(30))).scalars().all()
            if rk:
                self.ranking_edition = rk[0].edition
                self.top_team_norms = {team_norm(r.team) for r in rk if r.edition == rk[0].edition}

    @staticmethod
    def _row_dict(r: Match) -> dict:
        return {k: getattr(r, k) for k in ("key", "team1", "team2", "event", "stage", "best_of", "status", "start_time",
                                           "score1", "score2", "maps", "current_map", "tier", "importance", "source",
                                           "source_ids", "url", "stats", "confidence", "sync_status", "updated_at",
                                           "finished_at")}

    # -------------------------------------------------------------- polling
    async def poll(self, src: str, results: bool = False) -> int:
        if not self.source_ready(src):
            return 0
        try:
            if src == "liquipedia":
                items = await self.liquipedia.matches()
            elif src == "hltv":
                items = await (self.hltv.results() if results else self.hltv.matches())
            elif src == "pandascore":
                items = await (self.pandascore.results() if results else self.pandascore.matches())
            elif src == "faceit":
                items = await self.faceit.matches()
            else:
                return 0
        except SourceError as e:
            log.info("Esports %s: %s", src, e)
            return 0
        now = time.time()
        bucket = self.obs.setdefault(src, {})
        for m in items:
            m.fetched_at = now
            bucket[m.source_id] = m
        # forget old observations from this source
        for sid in [k for k, v in bucket.items() if now - v.fetched_at > 6 * 3600]:
            bucket.pop(sid, None)
        if not results:
            self.obs_ts[src] = now
        await self.reconcile({(src, m.source_id) for m in items})
        return len(items)

    def _find_key(self, m: LiveMatch, pending: dict[str, dict] | None = None) -> str:
        a, b = sorted([team_norm(m.team1), team_norm(m.team2)])
        window = float(self.ecfg.get("match_window_h", 8)) * 3600
        for key, cur in list(self.matches.items()) + list((pending or {}).items()):
            ca, cb = sorted([team_norm(cur["team1"]), team_norm(cur["team2"])])
            if (ca, cb) != (a, b) or "tbd" in (a, b):
                continue
            st, ct = m.start_time, cur.get("start_time")
            if st and ct and abs(st - ct) <= window:
                return key
            if not st or not ct:
                if cur["status"] in ("live", "upcoming") and m.status != "upcoming":
                    return key
        day = time.strftime("%Y%m%d%H", time.gmtime(m.start_time or time.time()))
        return f"{a}-{b}-{day}"

    async def reconcile(self, touched: set[tuple[str, str]] | None = None) -> list[MatchEvent]:
        now = time.time()
        groups: dict[str, list[LiveMatch]] = {}
        pending: dict[str, dict] = {}
        for src in self.priority() + [s for s in self.obs if s not in self.priority()]:
            for sid, m in (self.obs.get(src) or {}).items():
                key = self._find_key(m, pending)
                if key not in self.matches and key not in pending:
                    pending[key] = {"team1": m.team1, "team2": m.team2, "start_time": m.start_time, "status": m.status}
                groups.setdefault(key, []).append(m)
        events: list[MatchEvent] = []
        prio = self.priority()
        for key, obs in groups.items():
            def rank(o: LiveMatch) -> tuple:
                fresh = now - o.fetched_at <= self.stale_after(o.source)
                return (0 if fresh else 1, 1 if o.inferred else 0, prio.index(o.source) if o.source in prio else 99)
            obs.sort(key=rank)
            best = obs[0]
            fresh_obs = [o for o in obs if now - o.fetched_at <= self.stale_after(o.source) and not o.inferred]
            sync = "ok"
            if len({(o.score1, o.score2) if team_norm(o.team1) == team_norm(best.team1) else (o.score2, o.score1)
                     for o in fresh_obs if o.status != "upcoming"}) > 1:
                sync = "conflict"
                if key not in self._conflicts_logged:
                    self._conflicts_logged.add(key)
                    log.warning("Score conflict in %s: %s", key,
                                [(o.source, o.score1, o.score2) for o in fresh_obs])
            if best.inferred:
                sync = "inferred"
            if now - best.fetched_at > self.stale_after(best.source):
                sync = "stale"
            # A final result from any reliable source closes the match
            finished = next((o for o in obs if o.status == "finished"), None)
            if finished and best.status != "finished" and finished.fetched_at >= best.fetched_at - 300:
                best = finished
            conf = self.base_confidence(best.source) * (1.0 if sync in ("ok", "conflict") else 0.4)
            if sync == "conflict":
                conf *= 0.8
            new = self._to_dict(key, best, obs, sync, conf, now)
            prev = self.matches.get(key)
            events.extend(self._diff(prev, new))
            self.matches[key] = new
            if self._changed(prev, new):
                await self._persist(new)
        # flag as stale the live matches nobody confirms anymore
        for key, cur in self.matches.items():
            if cur["status"] == "live" and now - (cur.get("updated_at") or 0) > 1800 and cur["sync_status"] != "stale":
                cur["sync_status"] = "stale"
                await self._persist(cur)
        if events and self.on_events:
            await self.on_events(events)
        return events

    @staticmethod
    def _changed(prev: dict | None, new: dict) -> bool:
        if prev is None:
            return True
        keys = ("status", "score1", "score2", "maps", "current_map", "sync_status", "source", "event", "start_time")
        if any(prev.get(k) != new.get(k) for k in keys):
            return True
        return (new.get("updated_at") or 0) - (prev.get("updated_at") or 0) >= 120

    def _to_dict(self, key: str, m: LiveMatch, obs: list[LiveMatch], sync: str, conf: float, now: float) -> dict:
        prev = self.matches.get(key) or {}
        importance = self.importance(m)
        return {
            "key": key, "team1": m.team1, "team2": m.team2, "event": m.event or prev.get("event", ""),
            "stage": m.stage or prev.get("stage", ""), "best_of": m.best_of or 1, "status": m.status,
            "start_time": m.start_time or prev.get("start_time"), "score1": m.score1, "score2": m.score2,
            "maps": [x.as_dict() for x in m.maps] or (prev.get("maps") if m.source == prev.get("source") else []) or [],
            "current_map": m.current_map, "tier": m.tier, "importance": importance, "source": m.source,
            "source_ids": {o.source: o.source_id for o in obs}, "url": m.url or prev.get("url"),
            "stats": m.stats or prev.get("stats") or {}, "confidence": round(conf, 2), "sync_status": sync,
            "updated_at": m.fetched_at,
            # if we saw it finish, the detection time; if it arrived finished, its start time
            "finished_at": prev.get("finished_at") or (
                (now if prev.get("status") == "live" else m.start_time) if m.status == "finished" else None),
            "inferred": m.inferred, "has_live_score": m.has_live_score,
        }

    def importance(self, m: LiveMatch) -> float:
        score = {0: 60, 1: 40, 2: 15}.get(m.tier if m.tier is not None else 2, 10)
        if PLAYOFF_STAGE.search(f"{m.stage} {m.event}"):
            score += 20
        if "grand final" in f"{m.stage} {m.event}".lower():
            score += 15
        tops = sum(1 for t in (m.team1, m.team2) if team_norm(t) in self.top_team_norms)
        score += tops * 12
        return min(100.0, score)

    def _diff(self, prev: dict | None, new: dict) -> list[MatchEvent]:
        ev: list[MatchEvent] = []
        tn = (team_norm(new["team1"]), team_norm(new["team2"]))
        mk = lambda kind, detail="", map_name=None: MatchEvent(kind, new, detail, new["importance"], map_name, tn)
        if new.get("inferred") or new["sync_status"] == "stale":
            return ev  # never announce based on inferred states or stale data
        if prev is None:
            if new["status"] == "live":
                ev.append(mk("match_start"))
            return ev
        if prev["status"] != "live" and new["status"] == "live":
            ev.append(mk("match_start"))
        if new["status"] == "live" and new.get("current_map") and new["current_map"] != prev.get("current_map") \
                and new["current_map"] != "Por decidir":
            ev.append(mk("map_change", map_name=new["current_map"]))
        prev_done = {(m["name"], m.get("s1"), m.get("s2")) for m in prev.get("maps") or [] if m.get("status") == "done"}
        for mp in new.get("maps") or []:
            if mp.get("status") == "done" and (mp["name"], mp.get("s1"), mp.get("s2")) not in prev_done \
                    and new["status"] != "finished" and prev["status"] == "live":
                ev.append(mk("map_end", f"{mp.get('s1')}-{mp.get('s2')}", mp["name"]))
        if prev["status"] != "finished" and new["status"] == "finished" and prev["status"] == "live":
            ev.append(mk("match_end"))
        return ev

    async def _persist(self, d: dict) -> None:
        cols = {k: v for k, v in d.items() if k not in ("inferred", "has_live_score")}
        async with self.db.session() as s:
            row = (await s.execute(select(Match).where(Match.key == d["key"]))).scalar_one_or_none()
            if row is None:
                s.add(Match(**cols))
            else:
                for k, v in cols.items():
                    setattr(row, k, v)

    # ---------------------------------------------------------- queries
    def _sorted(self, status: str) -> list[dict]:
        items = [m for m in self.matches.values() if m["status"] == status]
        if status == "finished":
            return sorted(items, key=lambda m: -(m.get("finished_at") or m.get("start_time") or 0))
        if status == "live":
            return sorted(items, key=lambda m: -m["importance"])
        return sorted(items, key=lambda m: m.get("start_time") or 9e18)

    def live(self) -> list[dict]:
        return self._sorted("live")

    def upcoming(self, limit: int = 8) -> list[dict]:
        now = time.time()
        return [m for m in self._sorted("upcoming") if (m.get("start_time") or now) >= now - 3600][:limit]

    def finished(self, limit: int = 8) -> list[dict]:
        return self._sorted("finished")[:limit]

    def find(self, team: str, statuses: tuple = ("live", "upcoming", "finished")) -> dict | None:
        q = team_norm(team)
        if not q:
            return None
        for st in statuses:
            for m in self._sorted(st):
                t1, t2 = team_norm(m["team1"]), team_norm(m["team2"])
                if q in (t1, t2) or (len(q) >= 3 and (t1.startswith(q) or t2.startswith(q))):
                    return m
        return None

    async def match_stats(self, m: dict) -> dict:
        if m.get("stats"):
            return m["stats"]
        fid = (m.get("source_ids") or {}).get("faceit")
        if fid and self.source_ready("faceit"):
            try:
                st = await self.faceit.stats(fid)
                if st:
                    m["stats"] = st
                return st
            except SourceError:
                pass
        return {}

    # ------------------------------------------------------- follow
    async def follow(self, network: str, target: str, who: str, team: str) -> bool:
        tn = team_norm(team)
        async with self.db.session() as s:
            exists = (await s.execute(select(MatchFollow).where(and_(MatchFollow.network == network,
                      MatchFollow.target == target, MatchFollow.team_norm == tn)))).scalar_one_or_none()
            if exists:
                return False
            s.add(MatchFollow(network=network, target=target, requested_by=who, team=team, team_norm=tn,
                              created_at=time.time()))
            return True

    async def unfollow(self, network: str, target: str, team: str) -> bool:
        tn = team_norm(team)
        async with self.db.session() as s:
            row = (await s.execute(select(MatchFollow).where(and_(MatchFollow.network == network,
                   MatchFollow.target == target, MatchFollow.team_norm == tn)))).scalar_one_or_none()
            if not row:
                return False
            await s.delete(row)
            return True

    async def follows(self, network: str | None = None, target: str | None = None) -> list[MatchFollow]:
        q = select(MatchFollow)
        if network:
            q = q.where(MatchFollow.network == network)
        if target:
            q = q.where(MatchFollow.target == target)
        async with self.db.session() as s:
            return list((await s.execute(q)).scalars().all())

    # ---------------------------------------------- teams / players / tournaments
    async def team(self, name: str) -> dict | None:
        return await self._cached(Team, name, self.liquipedia.team, key_attr="name")

    async def player(self, name: str) -> dict | None:
        return await self._cached(Player, name, self.liquipedia.player, key_attr="nick")

    async def _cached(self, model, name: str, fetch, key_attr: str) -> dict | None:
        n = norm(name)
        ttl = float(self.ecfg.get("profile_cache_h", 24)) * 3600
        async with self.db.session() as s:
            row = (await s.execute(select(model).where(model.norm == n))).scalars().first()
            if row and time.time() - row.updated_at < ttl:
                return row.data
        if not self.source_ready("liquipedia"):
            return row.data if row else None
        try:
            data = await fetch(name)
        except SourceError as e:
            log.info("Liquipedia profile %s: %s", name, e)
            return row.data if row else None
        if not data:
            return None
        async with self.db.session() as s:
            row = (await s.execute(select(model).where(model.norm == n))).scalars().first()
            if row:
                row.data, row.updated_at = data, time.time()
            else:
                kw = {key_attr: (data.get("name") if key_attr == "name" else data.get("id")) or name}
                exists = (await s.execute(select(model).where(getattr(model, key_attr) == kw[key_attr]))).scalars().first()
                if exists:
                    exists.data, exists.updated_at, exists.norm = data, time.time(), n
                else:
                    s.add(model(**kw, norm=n, data=data, source="liquipedia", updated_at=time.time()))
        return data

    async def refresh_tournaments(self) -> int:
        if not self.source_ready("liquipedia"):
            return 0
        items = await self.liquipedia.tournaments()
        async with self.db.session() as s:
            for t in items:
                row = (await s.execute(select(Tournament).where(Tournament.page == t["page"]))).scalar_one_or_none()
                if row:
                    row.name, row.status, row.is_major, row.updated_at = t["name"], t["status"], t["is_major"], time.time()
                else:
                    s.add(Tournament(page=t["page"], name=t["name"], status=t["status"], is_major=t["is_major"],
                                     source="liquipedia", updated_at=time.time()))
        return len(items)

    async def tournaments(self, status: str | None = None, major_only: bool = False) -> list[Tournament]:
        q = select(Tournament)
        if status:
            q = q.where(Tournament.status == status)
        if major_only:
            q = q.where(Tournament.is_major.is_(True))
        async with self.db.session() as s:
            return list((await s.execute(q.order_by(Tournament.status, Tournament.name))).scalars().all())

    async def refresh_ranking(self) -> int:
        if not self.source_ready("vrs"):
            return 0
        edition, rows = await self.vrs.latest()
        if not rows:
            return 0
        async with self.db.session() as s:
            existing = (await s.execute(select(Ranking).where(and_(Ranking.source == "vrs", Ranking.edition == edition)))).scalars().first()
            if not existing:
                for r in rows[:100]:
                    s.add(Ranking(source="vrs", edition=edition, position=r["position"], team=r["team"],
                                  points=r["points"], roster=r["roster"], updated_at=time.time()))
        self.ranking_edition = edition
        self.top_team_norms = {team_norm(r["team"]) for r in rows[:30]}
        return len(rows)

    async def ranking(self, limit: int = 10) -> tuple[str | None, list[Ranking]]:
        async with self.db.session() as s:
            ed = self.ranking_edition or (await s.execute(select(Ranking.edition).order_by(desc(Ranking.edition)).limit(1))).scalar()
            if not ed:
                return None, []
            rows = (await s.execute(select(Ranking).where(Ranking.edition == ed).order_by(Ranking.position).limit(limit))).scalars().all()
            return ed, list(rows)

    async def team_rank(self, name: str) -> Ranking | None:
        tn = team_norm(name)
        _, rows = await self.ranking(100)
        return next((r for r in rows if team_norm(r.team) == tn), None)
