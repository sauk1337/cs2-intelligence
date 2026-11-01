"""Smart IRC posting through an outbox that Eggdrop polls.

For every channel these checks apply, in order: global pause, silent mode, category enabled
(config + preferences saved with !alerts), minimum relevance, quiet hours (except urgent
priority), per-key cooldown and the posts-per-hour limit.
Everything suppressed is logged in `alert_log` with its reason (for !admin alerts).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select, update

from ..core.config import Config
from ..database.db import Database
from ..database.models import AlertLog, AlertPref, Outbox
from ..utils.text import irc_safe, split_irc

log = logging.getLogger("cs2.publisher")
CATEGORIES = ["news", "valve", "market", "opportunity", "arbitrage", "trend", "volume", "live", "results",
              "upcoming", "watch"]
# Groups for !alerts on|off <group> (each category can also be toggled on its own)
ALERT_GROUPS = {
    "market": ["market", "opportunity", "arbitrage", "trend", "volume", "watch"],
    "news": ["news", "valve"],
    "live": ["live", "results", "upcoming"],
}
URGENT = 9


@dataclass
class Decision:
    target: str
    ok: bool
    reason: str | None = None


class Publisher:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.paused = False

    async def load(self) -> None:
        self.paused = (await self.db.kv_get("publisher_paused", "0")) == "1"

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self.db.kv_set("publisher_paused", "1" if paused else "0")

    # ----------------------------------------------------------- config
    def networks(self) -> dict:
        return self.cfg.get("settings.networks", {}) or {}

    def channel_conf(self, network: str, channel: str) -> dict:
        defaults = self.cfg.get("settings.publishing.defaults", {}) or {}
        ch = ((self.networks().get(network) or {}).get("channels") or {})
        conf = ch.get(channel) or ch.get(channel.lower()) or {}
        return {**defaults, **(conf or {})}

    def channels(self) -> list[tuple[str, str]]:
        out = []
        for net, n in self.networks().items():
            for ch in (n or {}).get("channels", {}) or {}:
                out.append((net, ch))
        return out

    async def prefs(self, network: str, scope: str, target: str) -> dict[str, bool]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AlertPref).where(and_(AlertPref.network == network, AlertPref.scope == scope,
                                                                 AlertPref.target == target)))).scalars().all()
        return {r.category: r.enabled for r in rows}

    async def set_pref(self, network: str, scope: str, target: str, category: str, enabled: bool) -> None:
        async with self.db.session() as s:
            row = (await s.execute(select(AlertPref).where(and_(AlertPref.network == network, AlertPref.scope == scope,
                   AlertPref.target == target, AlertPref.category == category)))).scalar_one_or_none()
            if row:
                row.enabled = enabled
            else:
                s.add(AlertPref(network=network, scope=scope, target=target, category=category, enabled=enabled))

    async def category_enabled(self, network: str, channel: str, category: str) -> bool:
        conf = self.channel_conf(network, channel)
        p = await self.prefs(network, "channel", channel)
        if category in p:
            return p[category]
        if category == "watch":
            return True  # personal watchlist alerts are on unless muted with !alerts off watch
        return category in (conf.get("categories") or CATEGORIES)

    @staticmethod
    def in_quiet_hours(spec: str | None, tz: str, now: float | None = None) -> bool:
        if not spec or "-" not in spec:
            return False
        start, end = [s.strip() for s in spec.split("-", 1)]
        cur = datetime.fromtimestamp(now or time.time(), ZoneInfo(tz)).strftime("%H:%M")
        return start <= cur < end if start <= end else (cur >= start or cur < end)

    # --------------------------------------------------------- publish
    async def publish(self, category: str, lines: list[str], *, priority: int = 5, dedupe_key: str = "",
                      cooldown_s: float = 0, metric: float | None = None, metric_key: str | None = None,
                      targets: list[tuple[str, str]] | None = None, force_targets: bool = False) -> list[Decision]:
        tz = self.cfg.get("settings.bot.timezone", "Europe/Madrid")
        now = time.time()
        decisions = []
        for network, channel in (targets or self.channels()):
            conf = self.channel_conf(network, channel)
            reason = None
            if self.paused:
                reason = "paused"
            elif conf.get("silent"):
                reason = "silent"
            elif not force_targets and not await self.category_enabled(network, channel, category):
                reason = "category_off"
            elif metric is not None and metric_key and metric < float(conf.get(metric_key, 0)):
                reason = "below_threshold"
            elif priority < URGENT and self.in_quiet_hours(conf.get("quiet_hours"), tz, now):
                reason = "quiet_hours"
            elif dedupe_key and cooldown_s and await self._recent(network, channel, dedupe_key, now - cooldown_s):
                reason = "cooldown"
            elif priority < URGENT and await self._count_hour(network, channel, now) >= int(conf.get("max_per_hour", 12)):
                reason = "hourly_limit"
            await self._log(network, channel, category, dedupe_key or category, priority, reason)
            if reason:
                decisions.append(Decision(channel, False, reason))
                continue
            await self.enqueue(network, channel, lines, category, priority, max_lines=int(conf.get("max_lines", 6)))
            decisions.append(Decision(channel, True))
        return decisions

    async def enqueue(self, network: str, target: str, lines: list[str], category: str = "", priority: int = 5,
                      kind: str = "PRIVMSG", max_lines: int = 8) -> int:
        max_bytes = int(self.cfg.get("settings.irc.max_line_bytes", 400))
        out = []
        for ln in lines:
            out.extend(split_irc(ln, max_bytes))
        out = out[:max_lines]
        t = time.time()
        async with self.db.session() as s:
            for i, ln in enumerate(out):
                s.add(Outbox(network=network, target=irc_safe(target)[:100], kind=kind, category=category,
                             priority=priority, text=ln, created_at=t + i * 1e-3))
        return len(out)

    async def _recent(self, network: str, target: str, key: str, since: float) -> bool:
        async with self.db.session() as s:
            r = (await s.execute(select(AlertLog.id).where(and_(AlertLog.network == network, AlertLog.target == target,
                 AlertLog.dedupe_key == key, AlertLog.ts >= since, AlertLog.suppressed.is_(None))).limit(1))).first()
        return r is not None

    async def _count_hour(self, network: str, target: str, now: float) -> int:
        async with self.db.session() as s:
            return (await s.execute(select(func.count(AlertLog.id)).where(and_(AlertLog.network == network,
                    AlertLog.target == target, AlertLog.ts >= now - 3600, AlertLog.suppressed.is_(None))))).scalar() or 0

    async def _log(self, network, target, category, key, priority, reason) -> None:
        async with self.db.session() as s:
            s.add(AlertLog(ts=time.time(), network=network, target=target, category=category, dedupe_key=key[:200],
                           priority=priority, suppressed=reason))

    # ------------------------------------------------- collection (Eggdrop)
    async def fetch(self, network: str, limit: int = 10) -> list[Outbox]:
        """Deliver at most `limit` messages and mark them delivered (at-most-once)."""
        stale = float(self.cfg.get("settings.publishing.outbox_max_age_s", 900))
        now = time.time()
        async with self.db.session() as s:
            rows = (await s.execute(select(Outbox).where(and_(Outbox.network == network, Outbox.delivered_at.is_(None),
                    Outbox.created_at >= now - stale)).order_by(Outbox.priority.desc(), Outbox.id).limit(limit))).scalars().all()
            if rows:
                await s.execute(update(Outbox).where(Outbox.id.in_([r.id for r in rows])).values(delivered_at=now))
            # drop what went stale (never post old information as current)
            await s.execute(update(Outbox).where(and_(Outbox.network == network, Outbox.delivered_at.is_(None),
                            Outbox.created_at < now - stale)).values(delivered_at=-1))
        return sorted(rows, key=lambda r: (-r.priority, r.id))

    async def stats(self, hours: float = 24) -> dict:
        since = time.time() - hours * 3600
        async with self.db.session() as s:
            rows = (await s.execute(select(AlertLog.category, AlertLog.suppressed, func.count(AlertLog.id))
                                    .where(AlertLog.ts >= since).group_by(AlertLog.category, AlertLog.suppressed))).all()
        out: dict = {}
        for cat, sup, n in rows:
            d = out.setdefault(cat, {"sent": 0, "suppressed": {}})
            if sup:
                d["suppressed"][sup] = d["suppressed"].get(sup, 0) + n
            else:
                d["sent"] += n
        return out
