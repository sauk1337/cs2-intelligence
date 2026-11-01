"""Alert engine: decides WHAT to post (news, opportunities, watchlists, LIVE, trends)."""
from __future__ import annotations

import logging
import time

from sqlalchemy import and_, select

from ..core.config import Config
from ..database.db import Database
from ..database.models import IrcUser, News, Watch
from ..esports.service import EsportsService, MatchEvent
from ..irc.format import Formatter
from ..market.service import Evaluation, MarketService
from ..news.service import NewsService
from ..utils.text import money, pct, pct_change, team_norm
from .publisher import URGENT, Publisher

log = logging.getLogger("cs2.alerts")


class AlertEngine:
    def __init__(self, cfg: Config, db: Database, pub: Publisher, fmt: Formatter, news: NewsService,
                 market: MarketService, esports: EsportsService) -> None:
        self.cfg, self.db, self.pub, self.fmt = cfg, db, pub, fmt
        self.news, self.market, self.esports = news, market, esports

    @property
    def acfg(self) -> dict:
        return self.cfg.get("settings.alerts", {}) or {}

    def cooldown(self, key: str, default: float) -> float:
        return float((self.acfg.get("cooldowns_s") or {}).get(key, default))

    # --------------------------------------------------------------- news
    async def on_news(self, items: list[News]) -> None:
        max_age = float(self.cfg.get("settings.news.publish_max_age_h", 6)) * 3600
        for n in sorted(items, key=lambda n: n.published_at):
            if n.duplicate_of is not None or n.posted_at:
                continue
            if time.time() - n.published_at > max_age:
                continue  # kept in the history, but not announced as new
            official_update = n.category in ("actualizacion", "parche") and n.source in self.news.official_sources()
            if official_update:
                analysis = self.news.valve_analysis(n)
                lines = self.fmt.valve_update(n, analysis)
                res = await self.pub.publish("valve", lines, priority=8, dedupe_key=f"news:{n.id}", cooldown_s=86400)
            else:
                res = await self.pub.publish("news", self.fmt.news(n), priority=7 if n.important else 4,
                                             dedupe_key=f"news:{n.id}", cooldown_s=86400, metric=n.relevance,
                                             metric_key="min_news_relevance")
            if any(d.ok for d in res):
                async with self.db.session() as s:
                    row = await s.get(News, n.id)
                    row.posted_at = time.time()

    # ------------------------------------------------------- opportunities
    async def on_analysis(self, evals: list[Evaluation]) -> None:
        labels = set(self.acfg.get("publish_labels", ["EXCELLENT", "INTERESTING"]))
        drops = sorted([e for e in evals if e.score.eligible and e.score.label in labels], key=lambda e: -e.score.score)
        for e in drops[: int(self.acfg.get("max_opportunities_per_run", 3))]:
            await self.pub.publish("opportunity", self.fmt.opportunity(e), priority=6,
                                   dedupe_key=f"opp:{e.item.id}", cooldown_s=self.cooldown("opportunity", 12 * 3600),
                                   metric=e.score.score, metric_key="min_opportunity_score")
        arbs = sorted([(e, e.arbitrage[0]) for e in evals if e.arbitrage], key=lambda x: -x[1].score)
        for e, a in arbs[: int(self.acfg.get("max_arbitrage_per_run", 2))]:
            if a.label in labels:
                await self.pub.publish("arbitrage", self.fmt.arbitrage(e, a), priority=5,
                                       dedupe_key=f"arb:{e.item.id}:{a.buy_source}:{a.sell_source}",
                                       cooldown_s=self.cooldown("arbitrage", 12 * 3600), metric=a.score,
                                       metric_key="min_opportunity_score")
        # abnormal drops and unusual volume (across the whole market)
        th_drop = float(self.acfg.get("abnormal_drop_pct", 20))
        for r in self.market.top_lists("drops", 5):
            if r["vs30"] <= -th_drop and r["vol30"] >= 30:
                f = self.fmt
                await self.pub.publish("market", [
                    f"{f.tag('alert')} {f.b(f.c('Abnormal drop:', 'red'))} {f.b(r['ref'].parsed.short)} {f.price(r['price'])} "
                    f"({f.chg(r['vs30'])} vs 30d median {money(r['med30'])}){f.sep}{r['vol30']} sales/30d{f.sep}"
                    f"{f.c('check the listing before acting', 'orange')}  {f.link(r['url'])}"],
                    priority=5, dedupe_key=f"drop:{r['ref'].id}", cooldown_s=self.cooldown("market", 24 * 3600))
        for r in self.market.top_lists("volume", 3):
            if (r["vol_ratio"] or 0) >= float(self.acfg.get("volume_spike_ratio", 3)) and r["vol7"] >= 30:
                f = self.fmt
                await self.pub.publish("volume", [
                    f"{f.tag('alert')} {f.b(f.c('Unusual volume:', 'yellow'))} {f.b(r['ref'].parsed.short)} — "
                    f"{f.b(r['vol24'])} sales in 24h ({f.c('x' + format(r['vol_ratio'], '.1f'), 'green')} its daily average)"
                    f"{f.sep}{f.k('price')} {f.price(r['price'])}  {f.link(r['url'])}"],
                    priority=4, dedupe_key=f"vol:{r['ref'].id}", cooldown_s=self.cooldown("volume", 24 * 3600))

    async def trend_digest(self) -> None:
        g = self.market.top_lists("gainers", 3)
        lo = self.market.top_lists("losers", 3)
        if not g and not lo:
            return
        f = self.fmt
        cur = self.market.base_currency
        lines = [f"{f.tag('trend')} {f.b('Market trends')} {f.c('(7d vs 30d median sale, liquid items only)', 'grey')}",
                 f.b(f.c("▲ Up:", "green")) + " " + " | ".join(f.quick_row(r, "t7_30", cur) for r in g),
                 f.b(f.c("▼ Down:", "red")) + " " + " | ".join(f.quick_row(r, "t7_30", cur) for r in lo)]
        await self.pub.publish("trend", lines, priority=3, dedupe_key="trend_digest",
                               cooldown_s=self.cooldown("trend", 6 * 3600))

    # -------------------------------------------------------------- watchlist
    async def check_watchlists(self) -> int:
        sent = 0
        async with self.db.session() as s:
            rows = (await s.execute(select(Watch, IrcUser).join(IrcUser, Watch.user_id == IrcUser.id))).all()
        for w, u in rows:
            ref = self.market.by_id.get(w.item_id)
            if not ref:
                continue
            lat = self.market.latest.get(w.item_id) or {}
            pick = lat.get(self.market.reference_source) or next((v for v in lat.values() if v.get("price")), None)
            if not pick or pick.get("price") is None:
                continue
            price = pick["price"]
            msg = None
            crossed = w.baseline_price is None or w.baseline_price > w.target_price if w.target_price else False
            f = self.fmt
            if w.target_price and price <= w.target_price and crossed:  # alert when crossing the target
                msg = (f"{f.tag('watch')} {f.b(u.last_nick)}: {f.b(ref.parsed.short)} hit your target price "
                       f"{money(w.target_price)} {f.c('→', 'grey')} {f.b(f.c(money(price), 'green'))}  {f.link(pick.get('url'))}")
            elif w.baseline_price:
                ch = pct_change(price, w.baseline_price)
                if ch is not None and abs(ch) >= w.threshold_pct:
                    msg = (f"{f.tag('watch')} {f.b(u.last_nick)}: {f.b(ref.parsed.short)} {f.b(f.chg(ch))} since "
                           f"{money(w.baseline_price)} {f.c('→', 'grey')} {f.price(price)}  {f.link(pick.get('url'))}")
            if not msg:
                if not w.baseline_price:
                    async with self.db.session() as s:
                        (await s.get(Watch, w.id)).baseline_price = price
                continue
            # the user can mute their own watchlist alerts (!alerts off watch, in private)
            if not (await self.pub.prefs(u.network, "user", u.ident)).get("watch", True):
                continue
            target = w.channel or u.last_nick
            # if the channel muted watchlist alerts, deliver privately instead
            if w.channel and (await self.pub.prefs(u.network, "channel", w.channel)).get("watch") is False:
                target = u.last_nick
            await self.pub.enqueue(u.network, target, [msg], category="watch", priority=7)
            async with self.db.session() as s:
                row = await s.get(Watch, w.id)
                row.baseline_price, row.last_notified_at = price, time.time()
            sent += 1
        return sent

    # --------------------------------------------------------------- LIVE
    async def on_match_events(self, events: list[MatchEvent]) -> None:
        follows = await self.esports.follows()
        for e in events:
            lines = self.fmt.event(e)
            category = "results" if e.kind == "match_end" else "live"
            mk = e.match["key"]
            key = f"{e.kind}:{mk}:{e.map_name or ''}:{e.detail}"
            pri = 8 if e.importance >= 75 else 6
            await self.pub.publish(category, lines, priority=pri, dedupe_key=key, cooldown_s=6 * 3600,
                                   metric=e.importance, metric_key="min_live_importance")
            targets = {(f.network, f.target) for f in follows if f.team_norm in e.teams_norm}
            if targets:  # channels following a team: always notified
                await self.pub.publish(category, lines, priority=URGENT if e.kind == "match_end" else 8,
                                       dedupe_key="follow:" + key, cooldown_s=6 * 3600, targets=sorted(targets),
                                       force_targets=True)

    async def upcoming_reminders(self) -> None:
        lead = float(self.acfg.get("upcoming_lead_min", 15)) * 60
        min_imp = float(self.acfg.get("upcoming_min_importance", 60))
        follows = await self.esports.follows()
        now = time.time()
        for m in self.esports.upcoming(30):
            st = m.get("start_time")
            if not st or not (0 <= st - now <= lead):
                continue
            tn = (team_norm(m["team1"]), team_norm(m["team2"]))
            fm = self.fmt
            line = [f"{fm.tag('esports')} {fm.b(fm.c('Starting in ' + str(int((st - now) // 60)) + ' min:', 'orange'))} "
                    f"{fm.b(m['team1'])} {fm.c('vs', 'grey')} {fm.b(m['team2'])} {fm.c('(BO' + str(m['best_of']) + ')', 'grey')}"
                    f"{fm.sep}{m['event'][:60]}"]
            if m["importance"] >= min_imp:
                await self.pub.publish("upcoming", line, priority=5, dedupe_key=f"up:{m['key']}", cooldown_s=86400)
            targets = sorted({(f.network, f.target) for f in follows if f.team_norm in tn})
            if targets:
                await self.pub.publish("upcoming", line, priority=7, dedupe_key=f"fup:{m['key']}", cooldown_s=86400,
                                       targets=targets, force_targets=True)
