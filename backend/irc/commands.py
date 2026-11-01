"""IRC command dispatcher. Receives the context from Eggdrop and returns reply lines."""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import and_, select

from ..alerts.publisher import ALERT_GROUPS, CATEGORIES
from ..core.logging_setup import fmt_ts, recent
from ..database.maintenance import backup
from ..database.models import CommandLog, IrcUser, Watch
from ..news.classify import GROUPS
from ..utils.text import ago, downsample, irc_safe, money, pct, sparkline
from .format import Formatter

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger("cs2.commands")
VERSION = "1.1.0"


@dataclass
class Ctx:
    network: str
    channel: str          # "" for private messages
    nick: str
    handle: str = "*"
    is_admin: bool = False
    command: str = ""
    args: str = ""

    @property
    def ident(self) -> str:
        return self.handle if self.handle not in ("", "*") else f"nick:{self.nick.lower()}"

    @property
    def where(self) -> str:
        return self.channel or self.nick


@dataclass
class Reply:
    lines: list[str] = field(default_factory=list)
    notice: bool = False   # reply by NOTICE to the user instead of the channel


HELP = {
    "general": "!cs2 !help [topic] !status !uptime !version !about !more",
    "news": "!news [latest|esports|market|updates|major]",
    "market": "!market [top|drops|gainers|losers|volume|opportunities|arbitrage|trending|lowliq] · !marketstats",
    "skins": "!skin <name> · !skinprice <name> · !skintrend <name> · !skincompare <name> · !skinscore <name> · "
             "!watch <name> [5%] [<price] · !unwatch <name> · !watchlist",
    "esports": "!matches · !results · !tournaments · !major · !ranking · !teams · !team <name> · !player <nick>",
    "live": "!live [team] · !score <team> · !match <team> · !matchstats <team> · !upcoming · !finished · "
            "!followmatch <team> · !unfollowmatch <team> · !livealerts",
    "economy": "!cases · !stickers · !collections · !marketstats",
    "alerts": "!alerts · !alerts <on|off> <market|news|live|all|category>  (channel settings need admin)",
    "admin": "!admin status|reload|sources|alerts|config|logs|pause|resume|backup|refresh <task>",
}


class CommandRouter:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.started = time.time()
        self.pages: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self.rate: dict[tuple[str, str], deque] = defaultdict(deque)
        self.handlers = {
            "cs2": self.c_about, "about": self.c_about, "help": self.c_help, "status": self.c_status,
            "uptime": self.c_uptime, "version": self.c_version, "more": self.c_more,
            "news": self.c_news,
            "market": self.c_market, "marketstats": self.c_marketstats, "cases": self.c_cases,
            "stickers": self.c_stickers, "collections": self.c_collections,
            "skin": self.c_skin, "skinprice": self.c_skinprice, "skintrend": self.c_skintrend,
            "skincompare": self.c_skincompare, "skinscore": self.c_skinscore,
            "skinwatch": self.c_watch, "watch": self.c_watch, "unwatch": self.c_unwatch, "watchlist": self.c_watchlist,
            "matches": self.c_upcoming, "upcoming": self.c_upcoming, "results": self.c_finished,
            "finished": self.c_finished, "tournaments": self.c_tournaments, "major": self.c_major,
            "ranking": self.c_ranking, "teams": self.c_teams, "team": self.c_team, "player": self.c_player,
            "live": self.c_live, "score": self.c_score, "match": self.c_match, "matchstats": self.c_matchstats,
            "followmatch": self.c_follow, "unfollowmatch": self.c_unfollow, "livealerts": self.c_livealerts,
            "alerts": self.c_alerts, "admin": self.c_admin,
        }

    @property
    def fmt(self) -> Formatter:
        return self.app.fmt

    @property
    def page_size(self) -> int:
        return int(self.app.cfg.get("settings.irc.page_lines", 5))

    # ---------------------------------------------------------------- entry
    async def handle(self, ctx: Ctx) -> Reply:
        t0 = time.monotonic()
        ctx.command = re.sub(r"[^a-z0-9]", "", ctx.command.lower().lstrip("!"))[:20]
        ctx.args = irc_safe(ctx.args).strip()[:200]
        ok = True
        if not self._allow(ctx):
            return Reply(["Slow down a little, please wait a few seconds."], notice=True)
        handler = self.handlers.get(ctx.command)
        if handler is None:
            return Reply(["Unknown command. Try !help"], notice=True)
        try:
            reply = await handler(ctx)
        except Exception as e:  # never send tracebacks to IRC
            ok = False
            log.exception("Error in !%s %s", ctx.command, ctx.args)
            reply = Reply([f"Internal error while running !{ctx.command}. It has been logged ({type(e).__name__})."])
        reply = self._paginate(ctx, reply)
        try:
            async with self.app.db.session() as s:
                s.add(CommandLog(ts=time.time(), network=ctx.network, channel=ctx.channel or "(private)", nick=ctx.nick,
                                 handle=ctx.handle, command=ctx.command, args=ctx.args, ok=ok,
                                 latency_ms=(time.monotonic() - t0) * 1000))
        except Exception:
            pass
        return reply

    def _allow(self, ctx: Ctx) -> bool:
        limit = int(self.app.cfg.get("settings.irc.user_rate_limit", 6))
        window = float(self.app.cfg.get("settings.irc.user_rate_window_s", 30))
        if ctx.is_admin:
            return True
        q = self.rate[(ctx.network, ctx.nick.lower())]
        now = time.time()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _more_hint(self, n: int) -> str:
        return self.fmt.c(f"… {n} more line(s): !more", "grey")

    def _paginate(self, ctx: Ctx, reply: Reply) -> Reply:
        if ctx.command == "more" or len(reply.lines) <= self.page_size:
            return reply
        key = (ctx.network, ctx.nick.lower())
        head, rest = reply.lines[: self.page_size], reply.lines[self.page_size:]
        self.pages[key] = (time.time(), rest)
        head.append(self._more_hint(len(rest)))
        return Reply(head, reply.notice)

    async def c_more(self, ctx: Ctx) -> Reply:
        key = (ctx.network, ctx.nick.lower())
        ts, rest = self.pages.get(key, (0, []))
        if not rest or time.time() - ts > 600:
            return Reply(["Nothing more to show."], notice=True)
        head, rest = rest[: self.page_size], rest[self.page_size:]
        if rest:
            self.pages[key] = (time.time(), rest)
            head.append(self._more_hint(len(rest)))
        else:
            self.pages.pop(key, None)
        return Reply(head)

    def title(self, tag: str, text: str) -> str:
        return f"{self.fmt.tag(tag)} {self.fmt.b(text)}"

    @staticmethod
    def rows(items: list[str], per_line: int = 3, sep: str = " | ") -> list[str]:
        return [sep.join(items[i:i + per_line]) for i in range(0, len(items), per_line)]

    # -------------------------------------------------------------- general
    async def c_about(self, ctx: Ctx) -> Reply:
        name = self.app.cfg.get("settings.bot.name", "CS2Intel")
        f = self.fmt
        return Reply([f"{f.tag('esports')} {f.b(name)} — Counter-Strike 2 analyst: news, skin market intelligence "
                      f"(history, drops, opportunities, arbitrage), esports and LIVE matches. Type {f.b('!help')}. "
                      + f.c("Market indicators are informational, not financial advice.", "grey")])

    async def c_help(self, ctx: Ctx) -> Reply:
        topic = ctx.args.lower().strip()
        f = self.fmt
        if topic and topic in HELP:
            return Reply([f"{f.b(f.c(topic.upper(), 'lblue'))} {HELP[topic]}"], notice=True)
        return Reply([f"{f.b(f.c(k.upper(), 'lblue'))} {v}" for k, v in HELP.items() if k != "admin" or ctx.is_admin],
                     notice=True)

    async def c_version(self, ctx: Ctx) -> Reply:
        return Reply([f"{self.fmt.b('CS2 Intelligence Bot')} v{VERSION} · Python backend · Eggdrop/Tcl"])

    async def c_uptime(self, ctx: Ctx) -> Reply:
        up = int(time.time() - self.started)
        return Reply([f"Backend up for {self.fmt.b(f'{up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m')}"])

    async def c_status(self, ctx: Ctx) -> Reply:
        f = self.fmt
        h = self.app.health.all()
        ok = [x.name for x in h if x.status == "ok"]
        bad = [f"{x.name}:{x.status}" for x in h if x.status in ("down", "blocked", "degraded")]
        ms = self.app.market.market_stats()
        live = len(self.app.esports.live())
        posting = f.c("PAUSED", "red") if self.app.publisher.paused else f.c("active", "green")
        return Reply([f"{f.k('Sources OK')} {f.b(f.c(len(ok), 'green'))} ({', '.join(ok[:8])})"
                      + (f"{f.sep}{f.k('Issues')} {f.c(', '.join(bad), 'orange')}" if bad else ""),
                      f"{f.k('Market')} {f.b(ms['items'])} items, {f.b(ms['analyzed'])} analysable{f.sep}"
                      f"{f.k('Last analysis')} {ago(ms['last_analysis'])}{f.sep}{f.k('Live matches')} {f.b(live)}"
                      f"{f.sep}{f.k('Auto posts')} {posting}"])

    # ----------------------------------------------------------------- news
    async def c_news(self, ctx: Ctx) -> Reply:
        arg = ctx.args.lower().split()[0] if ctx.args else "latest"
        if arg not in ("latest", "major", *GROUPS):
            return Reply(["Usage: " + HELP["news"]], notice=True)
        items = await self.app.news.latest(group=arg if arg in GROUPS else None, limit=8, major=arg == "major")
        if not items:
            return Reply(["No stored news for that filter yet."])
        return Reply([self.fmt.news_compact(n) for n in items])

    # --------------------------------------------------------------- market
    async def c_market(self, ctx: Ctx) -> Reply:
        f = self.fmt
        sub = (ctx.args.lower().split() or ["top"])[0]
        cur = self.app.market.base_currency
        if sub in ("opportunities", "opps", "opp"):
            from .format import LABEL_STYLE
            rows = [(r, o) for r, o in await self.app.market.recent_opportunities("drop", 24, 12)
                    if o.label in LABEL_STYLE][:8]  # skip records stored before the English switch
            if not rows:
                return Reply(["No opportunities above the threshold in the last 24h."])
            return Reply([self.title("opp", "Opportunities (last 24h)")] + [
                f"{f.label(o.label)} {f.b(ref.parsed.short)} {money(o.price, cur)}{f.sep}{f.score(o.score)}{f.sep}"
                f"{f.k('risk')} {f.risk(o.risk)}{f.sep}{f.c((o.details or {}).get('reason', '')[:110], 'grey')}"
                for ref, o in rows])
        if sub in ("arbitrage", "arb"):
            from .format import LABEL_STYLE
            rows = [(r, o) for r, o in await self.app.market.recent_opportunities("arbitrage", 24, 10)
                    if o.label in LABEL_STYLE][:6]
            if not rows:
                return Reply(["No cross-platform gaps beating fees and the threshold (last 24h). "
                              "Enable more sources (CSFloat/Buff) to widen the comparison."])
            return Reply([self.title("arb", "Cross-platform gaps (last 24h)")] + [
                f"{f.label(o.label)} {f.b(ref.parsed.short)}: {f.src(o.source)} {money(o.price, cur)} {f.c('→', 'grey')} "
                f"{f.src(o.details.get('sell_source'))} ~{money(o.reference_price, cur)}{f.sep}{f.k('net')} {f.chg(o.change_pct)}"
                for ref, o in rows])
        kinds = {"top": ("top", "vs7", "Most traded (7d)"), "drops": ("drops", "vs30", "Biggest drops vs 30d median"),
                 "gainers": ("gainers", "t7_30", "Gainers (7d vs 30d median)"),
                 "losers": ("losers", "t7_30", "Losers (7d vs 30d median)"),
                 "volume": ("volume", "vol_ratio", "Volume spikes (24h vs 7d average)"),
                 "trending": ("trending", "t7_30", "Trending"), "lowliq": ("lowliq", "vs30", "Low liquidity")}
        if sub not in kinds:
            return Reply(["Usage: " + HELP["market"]], notice=True)
        kind, field_, title = kinds[sub]
        rows = self.app.market.top_lists(kind, 9)
        if not rows:
            return Reply(["Not enough market data yet (waiting for the first sync)."])
        if field_ == "vol_ratio":
            body = [f"{f.b(r['ref'].parsed.short)} {f.c('x' + format(r['vol_ratio'], '.1f'), 'green')} "
                    f"({r['vol24']} sales 24h) {money(r['price'], cur)}" for r in rows]
        else:
            body = [f.quick_row(r, field_, cur) for r in rows]
        return Reply([self.title("trend", title)] + self.rows(body))

    async def c_marketstats(self, ctx: Ctx) -> Reply:
        f = self.fmt
        s = self.app.market.market_stats()
        src = ", ".join(f.c(k, "green") if v else f.c(k + " (off)", "grey") for k, v in s["sources"].items())
        return Reply([f"{f.tag('market')} {f.b(s['items'])} items tracked{f.sep}{f.b(s['analyzed'])} with enough volume"
                      f"{f.sep}{f.k('median 7d/30d trend')} {f.chg(s['median_trend'])}{f.sep}"
                      f"{f.c('▲ ' + str(s['up']), 'green')} / {f.c('▼ ' + str(s['down']), 'red')}{f.sep}"
                      f"{f.b(s['volume7d'])} sales/7d (Skinport)",
                      f"{f.k('Sources:')} {src}{f.sep}{f.k('ECB rates')} {s['fx_date'] or 'n/a'}{f.sep}"
                      f"{f.k('Analysis')} {ago(s['last_analysis'])}"])

    async def _types(self, types, title, tournament_only=False) -> Reply:
        f = self.fmt
        rows = self.app.market.by_type(types, 9, tournament_only)
        if not rows:
            return Reply([f"Not enough data for {title.lower()} yet."])
        cur = self.app.market.base_currency
        body = [f"{f.b(r['ref'].parsed.short)} {money(r['price'], cur)} ({f.chg(r['t7_30'])}, {r['vol7']} sales/7d)"
                for r in rows]
        return Reply([self.title("trend", title)] + self.rows(body))

    async def c_cases(self, ctx: Ctx) -> Reply:
        return await self._types(("case",), "Cases on the move (7d vs 30d)")

    async def c_stickers(self, ctx: Ctx) -> Reply:
        return await self._types(("sticker", "capsule"), "Major stickers/capsules on the move", True)

    async def c_collections(self, ctx: Ctx) -> Reply:
        f = self.fmt
        rows = self.app.market.collections(8)
        if not rows:
            return Reply(["No collection data yet (needs metadata + sales statistics)."])
        body = [f"{f.b(r['name'].replace('The ', '').replace(' Collection', ''))} {f.chg(r['trend'])} ({r['n']} items)"
                for r in rows]
        return Reply([self.title("trend", "Collections (average 7d vs 30d trend)")] + self.rows(body, 4))

    # ---------------------------------------------------------------- skins
    async def _resolve(self, ctx: Ctx):
        if not ctx.args:
            return None, Reply([f"Usage: !{ctx.command} <name>  (e.g. !{ctx.command} ak redline ft)"], notice=True)
        refs = self.app.market.resolve(ctx.args)
        if not refs:
            return None, Reply([f"Can't find \"{ctx.args}\". Try weapon + skin (+ FN/MW/FT/WW/BS, st, souvenir)."])
        return refs[0], None

    async def c_skin(self, ctx: Ctx) -> Reply:
        from ..market.items import EXTERIOR_ABBR
        ref, err = await self._resolve(ctx)
        if err:
            return err
        ev = await self.app.market.evaluate(ref)
        variants = [EXTERIOR_ABBR.get(v.parsed.exterior or "", "") + (f" {v.parsed.phase}" if v.parsed.phase else "")
                    for v in self.app.market.variants(ref) if v.id != ref.id]
        return Reply(self.fmt.skin(ev, [v.strip() for v in variants if v.strip()][:6]))

    async def c_skinprice(self, ctx: Ctx) -> Reply:
        f = self.fmt
        ref, err = await self._resolve(ctx)
        if err:
            return err
        ev = await self.app.market.evaluate(ref)
        m = ev.metrics
        plats = f.sep.join(f"{f.src(s)} {f.price(p, m.currency)} {f.c('(' + ago(time.time() - a) + ')', 'grey')}"
                           for s, p, _u, a in m.platforms)
        return Reply([f"{f.tag('market')} {f.b(ref.parsed.short)}: {plats or 'no price available'}"
                      + (f"  {f.link(m.url)}" if m.url else "")])

    async def c_skintrend(self, ctx: Ctx) -> Reply:
        f = self.fmt
        ref, err = await self._resolve(ctx)
        if err:
            return err
        ev = await self.app.market.evaluate(ref)
        m = ev.metrics
        series = (await self.app.market.series(ref.id, 90)).get(m.source, [])
        spark = sparkline(downsample([p for _t, p in series], 30))
        url = self.app.chart_url(ref.id)
        lines = [f"{f.tag('trend')} {f.b(ref.parsed.short)}: {f.trend(m.trend_label)}{f.sep}{f.k('7d vs 30d')} "
                 f"{f.chg(m.trend_7_30)}{f.sep}{f.k('30d vs 90d')} {f.chg(m.trend_30_90)}",
                 f"{f.k('Change:')} {f.change_str(m)}"]
        if spark:
            lo, hi = min(p for _t, p in series), max(p for _t, p in series)
            lines.append(f"{f.k('Own history')} ({len(series)} points, {f.src(m.source)}): {f.c(spark, 'lblue')} "
                         f"{money(lo, m.currency)}–{money(hi, m.currency)}")
        else:
            lines.append(f.c("Own history still too short; trend based on the platform's sales statistics.", "grey"))
        if url:
            lines.append(f"{f.k('Chart:')} {url}")
        return Reply(lines)

    async def c_skincompare(self, ctx: Ctx) -> Reply:
        from ..analytics.arbitrage import net_if_sold
        f = self.fmt
        ref, err = await self._resolve(ctx)
        if err:
            return err
        ev = await self.app.market.evaluate(ref)
        m = ev.metrics
        if not m.platforms:
            return Reply([f"No recent prices for {ref.parsed.short}."])
        rows = net_if_sold(m, self.app.market.fees())
        lines = [f"{f.tag('market')} {f.b(ref.parsed.short)} — lowest price and net if you sell there:",
                 f.sep.join(f"{f.b(f.src(s))} {f.price(p, m.currency)} {f.c('→', 'grey')} {f.k('net')} {money(n, m.currency)}"
                            + ("" if cash else f.c(" (not cashable)", "orange")) for s, p, n, cash in rows)]
        if ev.arbitrage:
            a = ev.arbitrage[0]
            lines.append(f"{f.k('Best gap:')} buy {f.b(f.src(a.buy_source))} → sell {f.b(f.src(a.sell_source))}: "
                         f"{f.k('est. net')} {f.chg(a.profit_pct)} ({money(a.profit, m.currency)}){f.sep}"
                         f"{f.c('⚠ ' + ('; '.join(a.warnings) or 'no warnings'), 'orange')}{f.c(' · not a guaranteed profit', 'grey')}")
        else:
            lines.append(f.c("No price gap that beats the fees with the current data.", "grey"))
        return Reply(lines)

    async def c_skinscore(self, ctx: Ctx) -> Reply:
        ref, err = await self._resolve(ctx)
        if err:
            return err
        ev = await self.app.market.evaluate(ref)
        return Reply([f"{self.fmt.tag('opp')} {self.fmt.b(ref.parsed.short)}"] + self.fmt.score_breakdown(ev))

    # ------------------------------------------------------------ watchlist
    async def _user(self, ctx: Ctx) -> IrcUser:
        async with self.app.db.session() as s:
            u = (await s.execute(select(IrcUser).where(and_(IrcUser.network == ctx.network,
                                                            IrcUser.ident == ctx.ident)))).scalar_one_or_none()
            if not u:
                u = IrcUser(network=ctx.network, ident=ctx.ident, last_nick=ctx.nick, created_at=time.time(),
                            last_seen=time.time())
                s.add(u)
                await s.flush()
            else:
                u.last_nick, u.last_seen = ctx.nick, time.time()
            return u

    def _current_price(self, item_id: int):
        lat = self.app.market.latest.get(item_id, {})
        return (lat.get(self.app.market.reference_source) or next(iter(lat.values()), {})).get("price")

    async def c_watch(self, ctx: Ctx) -> Reply:
        f = self.fmt
        args = ctx.args
        threshold = float(self.app.cfg.get("settings.alerts.watch_default_pct", 5))
        target = None
        m = re.search(r"(?:^|\s)(\d+(?:[.,]\d+)?)%\s*", args)
        if m:
            threshold = max(1.0, min(90.0, float(m.group(1).replace(",", "."))))
            args = (args[: m.start()] + " " + args[m.end():]).strip()
        m = re.search(r"(?:^|\s)[<@](\d+(?:[.,]\d+)?)\s*", args)
        if m:
            target = float(m.group(1).replace(",", "."))
            args = (args[: m.start()] + " " + args[m.end():]).strip()
        ctx.args = args
        ref, err = await self._resolve(ctx)
        if err:
            return err
        u = await self._user(ctx)
        max_w = int(self.app.cfg.get("settings.alerts.max_watch_per_user", 25))
        price = self._current_price(ref.id)
        async with self.app.db.session() as s:
            count = len((await s.execute(select(Watch.id).where(Watch.user_id == u.id))).all())
            w = (await s.execute(select(Watch).where(and_(Watch.user_id == u.id, Watch.item_id == ref.id)))).scalar_one_or_none()
            if not w and count >= max_w:
                return Reply([f"Watchlist limit of {max_w} items reached."], notice=True)
            if w:
                w.threshold_pct, w.target_price, w.channel = threshold, target, ctx.channel
            else:
                s.add(Watch(user_id=u.id, item_id=ref.id, channel=ctx.channel, threshold_pct=threshold,
                            target_price=target, baseline_price=price, created_at=time.time()))
        self.app.market.queue_steam([ref.key], priority=2)
        extra = f" or if it drops below {f.b(money(target))}" if target else ""
        return Reply([f"{f.tag('watch')} Watching {f.b(ref.parsed.short)} (now {f.price(price)}): "
                      f"alert on a {f.b(f'±{threshold:g}%')} move{extra}."])

    async def c_unwatch(self, ctx: Ctx) -> Reply:
        ref, err = await self._resolve(ctx)
        if err:
            return err
        u = await self._user(ctx)
        async with self.app.db.session() as s:
            w = (await s.execute(select(Watch).where(and_(Watch.user_id == u.id, Watch.item_id == ref.id)))).scalar_one_or_none()
            if not w:
                return Reply([f"You weren't watching {ref.parsed.short}."], notice=True)
            await s.delete(w)
        return Reply([f"{self.fmt.tag('watch')} Stopped watching {self.fmt.b(ref.parsed.short)}."])

    async def c_watchlist(self, ctx: Ctx) -> Reply:
        f = self.fmt
        u = await self._user(ctx)
        async with self.app.db.session() as s:
            rows = (await s.execute(select(Watch).where(Watch.user_id == u.id))).scalars().all()
        if not rows:
            return Reply(["Your watchlist is empty. Add items with !watch <skin> [5%] [<price]"], notice=True)
        out = []
        for w in rows:
            ref = self.app.market.by_id.get(w.item_id)
            if not ref:
                continue
            price = self._current_price(w.item_id)
            ch = (price - w.baseline_price) / w.baseline_price * 100 if price and w.baseline_price else None
            out.append(f"{f.b(ref.parsed.short)} {f.price(price)} ({f.chg(ch)} since last alert){f.sep}±{w.threshold_pct:g}%"
                       + (f"{f.sep}{f.k('target')} {money(w.target_price)}" if w.target_price else ""))
        return Reply([f"{f.tag('watch')} {f.b(ctx.nick)}'s watchlist ({len(out)}):"] + out, notice=True)

    # -------------------------------------------------------------- esports
    async def c_live(self, ctx: Ctx) -> Reply:
        f = self.fmt
        if ctx.args:
            m = self.app.esports.find(ctx.args, ("live",))
            if not m:
                nxt = self.app.esports.find(ctx.args, ("upcoming",))
                return Reply([f"{f.b(ctx.args)} is not playing right now."
                              + (f" {f.k('Next:')} {f.upcoming_line(nxt)}" if nxt else "")])
            return Reply(f.match_full(m))
        live = self.app.esports.live()
        if not live:
            src = [s for s in self.app.esports.priority() if self.app.esports.source_ready(s)]
            return Reply([f"{f.tag('live')} No CS2 matches live right now "
                          + f.c(f"(active sources: {', '.join(src) or 'none'})", "grey")])
        return Reply([f.live_line(m) for m in live[:10]])

    async def c_score(self, ctx: Ctx) -> Reply:
        if not ctx.args:
            return Reply(["Usage: !score <team>"], notice=True)
        m = self.app.esports.find(ctx.args, ("live", "finished"))
        if not m:
            return Reply([f"No recent match for {ctx.args}."])
        return Reply([self.fmt.live_line(m) if m["status"] == "live" else self.fmt.finished_line(m)])

    async def c_match(self, ctx: Ctx) -> Reply:
        if not ctx.args:
            return Reply(["Usage: !match <team>"], notice=True)
        m = self.app.esports.find(ctx.args)
        return Reply(self.fmt.match_full(m) if m else [f"No matches found for {ctx.args}."])

    async def c_matchstats(self, ctx: Ctx) -> Reply:
        f = self.fmt
        if not ctx.args:
            return Reply(["Usage: !matchstats <team>"], notice=True)
        m = self.app.esports.find(ctx.args, ("live", "finished"))
        if not m:
            return Reply([f"No recent match for {ctx.args}."])
        st = await self.app.esports.match_stats(m)
        players = st.get("players") or []
        if not players:
            return Reply([f"{f.b(m['team1'])} vs {f.b(m['team2'])}: the source ({f.src(m['source'])}) does not "
                          f"provide player stats for this match."])
        lines = [f"{f.tag('esports')} Stats {f.b(m['team1'])} vs {f.b(m['team2'])} "
                 + f.c(f"({st.get('scope', '')}, {f.src(m['source'])})", "grey")]
        by_team: dict[str, list] = {}
        for p in players:
            if p.get("map") and st.get("maps") and p["map"] != st["maps"][-1]["map"]:
                continue
            by_team.setdefault(p.get("team") or "?", []).append(p)
        for team, ps in by_team.items():
            lines.append(f"{f.b(f.c(team, 'lblue'))}: " + f.c(" | ", "grey").join(
                f"{f.b(p['player'])} {p.get('kills', '?')}-{p.get('deaths', '?')}"
                + (f" A{p['assists']}" if p.get("assists") else "") + (f" ADR {p['adr']}" if p.get("adr") else "")
                + (f" KAST {p['kast']}" if p.get("kast") else "") + (f" R {p['rating']}" if p.get("rating") else "")
                + (f" MVP {p['mvps']}" if p.get("mvps") else "") for p in ps[:5]))
        return Reply(lines)

    async def c_upcoming(self, ctx: Ctx) -> Reply:
        rows = self.app.esports.upcoming(8)
        if not rows:
            return Reply(["No upcoming matches in the active sources."])
        tz = self.app.cfg.get("settings.bot.timezone", "Europe/Madrid")
        return Reply([self.title("esports", f"Upcoming matches ({tz} time)")] + [self.fmt.upcoming_line(m) for m in rows])

    async def c_finished(self, ctx: Ctx) -> Reply:
        rows = self.app.esports.finished(8)
        if not rows:
            return Reply(["No recent results in the active sources."])
        return Reply([self.title("esports", "Latest results")] + [self.fmt.finished_line(m) for m in rows])

    async def c_tournaments(self, ctx: Ctx) -> Reply:
        f = self.fmt
        rows = await self.app.esports.tournaments()
        if not rows:
            return Reply(["No tournament data (Liquipedia disabled or not synced yet)."])
        by = {"ongoing": [], "upcoming": [], "concluded": []}
        for t in rows:
            by.setdefault(t.status, []).append(t.name)
        out = []
        if by["ongoing"]:
            out.append(f"{f.b(f.c('● Ongoing:', 'red'))} " + ", ".join(by["ongoing"][:8]))
        if by["upcoming"]:
            out.append(f"{f.b(f.c('Upcoming:', 'lblue'))} " + ", ".join(by["upcoming"][:8]))
        if by["concluded"]:
            out.append(f"{f.b(f.c('Recent:', 'grey'))} " + ", ".join(by["concluded"][:6]))
        return Reply([self.title("esports", "Tournaments (Liquipedia)")] + out)

    async def c_major(self, ctx: Ctx) -> Reply:
        f = self.fmt
        rows = await self.app.esports.tournaments(major_only=True)
        names = [f"{f.b(t.name)} ({ {'ongoing': 'ongoing', 'upcoming': 'upcoming', 'concluded': 'finished'}.get(t.status, t.status)})"
                 for t in rows]
        live = [m for m in self.app.esports.live() if "major" in m["event"].lower()]
        news = await self.app.news.latest(limit=2, major=True)
        out = [f"{f.tag('esports')} {f.b('Majors:')} " + (", ".join(names) if names else "no data")]
        out += [f.live_line(m) for m in live[:3]]
        out += [f.news_compact(n) for n in news]
        return Reply(out)

    async def c_ranking(self, ctx: Ctx) -> Reply:
        f = self.fmt
        ed, rows = await self.app.esports.ranking(15)
        if not rows:
            return Reply(["Ranking not available yet."])
        medal = {1: "yellow", 2: "lgrey", 3: "orange"}
        body = [f"{f.b(f.c(f'#{r.position}', medal.get(r.position, 'lblue')))} {f.b(r.team)} {f.c(f'({r.points:.0f})', 'grey')}"
                for r in rows]
        return Reply([self.title("esports", f"Valve Regional Standings — global ({ed})")] + self.rows(body, 5, "  "))

    async def c_teams(self, ctx: Ctx) -> Reply:
        f = self.fmt
        ed, rows = await self.app.esports.ranking(10)
        if not rows:
            return Reply(["No team data."])
        return Reply([self.title("esports", f"Top teams (VRS {ed})")]
                     + [f"{f.b(f.c(f'#{r.position}', 'lblue'))} {f.b(r.team)}: {f.c(r.roster, 'grey')}" for r in rows[:10]])

    async def c_team(self, ctx: Ctx) -> Reply:
        f = self.fmt
        if not ctx.args:
            return Reply(["Usage: !team <name>"], notice=True)
        data = await self.app.esports.team(ctx.args)
        rank = await self.app.esports.team_rank(ctx.args) or (await self.app.esports.team_rank(data["name"]) if data else None)
        if not data and not rank:
            return Reply([f"Can't find the team \"{ctx.args}\"."])
        lines = []
        if data:
            players = ", ".join(f.b(p["id"]) + (f.c(" (IGL)", "orange") if p.get("igl") else "") for p in data.get("players", []))
            lines.append(f"{f.tag('esports')} {f.b(f.c(data['name'], 'lblue'))}{f.sep}{data.get('location') or '?'}{f.sep}"
                         f"{f.k('Roster:')} {players or 'n/a'}" + (f"{f.sep}{f.k('Coach:')} {data['coach']}" if data.get("coach") else ""))
        if rank:
            lines.append(f"{f.k('VRS:')} {f.b('#' + str(rank.position))} ({rank.points:.0f} pts, {rank.edition})")
        m = self.app.esports.find(ctx.args)
        if m:
            lines.append(f.live_line(m) if m["status"] == "live" else
                         f.upcoming_line(m) if m["status"] == "upcoming" else f.finished_line(m))
        if data:
            lines.append(f"{f.k('Source:')} Liquipedia  {f.link(data['url'])}")
        return Reply(lines)

    async def c_player(self, ctx: Ctx) -> Reply:
        f = self.fmt
        if not ctx.args:
            return Reply(["Usage: !player <nick>"], notice=True)
        p = await self.app.esports.player(ctx.args)
        if not p:
            return Reply([f"Can't find the player \"{ctx.args}\"."])
        return Reply([f"{f.tag('esports')} {f.b(f.c(p['id'], 'lblue'))} ({p.get('name') or '?'}){f.sep}{p.get('country') or '?'}"
                      f"{f.sep}{f.k('Team:')} {f.b(p.get('team') or 'free agent')}{f.sep}{f.k('Role:')} {p.get('roles') or 'n/a'}"
                      f"{f.sep}{f.k('Born:')} {p.get('birth_date') or 'n/a'}{f.sep}{f.k('Status:')} {p.get('status') or 'n/a'}",
                      f"{f.k('Source:')} Liquipedia  {f.link(p['url'])}"])

    async def c_follow(self, ctx: Ctx) -> Reply:
        if not ctx.args:
            return Reply(["Usage: !followmatch <team>"], notice=True)
        target = ctx.channel or ctx.nick
        ok = await self.app.esports.follow(ctx.network, target, ctx.ident, ctx.args)
        f = self.fmt
        return Reply([f"{f.tag('live')} Following {f.b(ctx.args)} in {target}: alerts for match start, maps and result."
                      if ok else f"{ctx.args} was already followed in {target}."])

    async def c_unfollow(self, ctx: Ctx) -> Reply:
        if not ctx.args:
            return Reply(["Usage: !unfollowmatch <team>"], notice=True)
        ok = await self.app.esports.unfollow(ctx.network, ctx.channel or ctx.nick, ctx.args)
        return Reply([f"Stopped following {self.fmt.b(ctx.args)}." if ok else f"{ctx.args} was not followed here."])

    async def c_livealerts(self, ctx: Ctx) -> Reply:
        f = self.fmt
        rows = await self.app.esports.follows(ctx.network, ctx.channel or ctx.nick)
        conf = self.app.publisher.channel_conf(ctx.network, ctx.channel) if ctx.channel else {}
        on = await self.app.publisher.category_enabled(ctx.network, ctx.channel, "live") if ctx.channel else False
        return Reply([f"{f.tag('live')} LIVE alerts in {ctx.where}: {self._onoff(on)} "
                      f"{f.c('(min importance ' + str(conf.get('min_live_importance', 'n/a')) + ')', 'grey')}{f.sep}"
                      f"{f.k('Followed teams:')} " + (", ".join(f.b(r.team) for r in rows) or "none")])

    # --------------------------------------------------------------- alerts
    def _onoff(self, on: bool) -> str:
        return self.fmt.b(self.fmt.c("ON", "green")) if on else self.fmt.b(self.fmt.c("OFF", "red"))

    async def c_alerts(self, ctx: Ctx) -> Reply:
        """!alerts · !alerts <on|off> <group|category> · !alerts <group|category> <on|off>"""
        f = self.fmt
        pub = self.app.publisher
        parts = ctx.args.lower().split()
        scope, target = ("channel", ctx.channel) if ctx.channel else ("user", ctx.ident)
        if not parts:
            if not ctx.channel:
                prefs = await pub.prefs(ctx.network, "user", ctx.ident)
                return Reply([f"Your personal alerts: {f.k('watch')} {self._onoff(prefs.get('watch', True))} "
                              + f.c("(your watchlist alerts). Use !alerts off watch to mute them.", "grey")], notice=True)
            lines = [f"{self.title('alert', 'Automatic alerts in ' + ctx.channel)}"]
            for group, cats in ALERT_GROUPS.items():
                states = [f"{c} {self._onoff(await pub.category_enabled(ctx.network, ctx.channel, c))}" for c in cats]
                lines.append(f"{f.b(f.c(group.upper(), 'lblue'))}: " + "  ".join(states))
            lines.append(f.c("Change with: !alerts <on|off> <market|news|live|all|category>  (admins)", "grey"))
            return Reply(lines)
        if len(parts) == 2 and parts[0] not in ("on", "off") and parts[1] in ("on", "off"):
            parts = [parts[1], parts[0]]
        valid = sorted(set(CATEGORIES) | set(ALERT_GROUPS) | {"all"})
        if len(parts) != 2 or parts[0] not in ("on", "off") or parts[1] not in valid:
            return Reply([f"Usage: !alerts <on|off> <{'|'.join(valid)}>"], notice=True)
        if ctx.channel and not ctx.is_admin:
            return Reply(["Changing this channel's alerts requires admin rights."], notice=True)
        enable = parts[0] == "on"
        what = parts[1]
        cats = list(CATEGORIES) if what == "all" else ALERT_GROUPS.get(what, [what])
        if scope == "user":
            cats = [c for c in cats if c == "watch"] or ["watch"]
        for c in cats:
            await pub.set_pref(ctx.network, scope, target, c, enable)
        return Reply([f"{f.tag('alert')} {self._onoff(enable)} {f.b(what)} alerts in {target}"
                      + (f" {f.c('(' + ', '.join(cats) + ')', 'grey')}" if len(cats) > 1 else "") + "."])

    # ---------------------------------------------------------------- admin
    async def c_admin(self, ctx: Ctx) -> Reply:
        f = self.fmt
        if not ctx.is_admin:
            return Reply(["Permission denied."], notice=True)
        parts = ctx.args.split()
        sub = parts[0].lower() if parts else "status"
        app = self.app
        if sub == "status":
            tasks = sorted(app.scheduler.tasks.values(), key=lambda t: t.name)
            failing = [f"{t.name}({t.consecutive_failures})" for t in tasks if t.consecutive_failures]
            db_ok = await app.db.ping()
            return Reply([f"{f.k('DB')} {f.c('OK', 'green') if db_ok else f.c('ERROR', 'red')}{f.sep}{f.k('tasks')} {len(tasks)}"
                          f"{f.sep}{f.k('failing')} {f.c(', '.join(failing), 'red') if failing else f.c('none', 'green')}"
                          f"{f.sep}{f.k('auto posts')} {f.c('PAUSED', 'red') if app.publisher.paused else f.c('active', 'green')}",
                          f"{f.k('Tasks:')} " + " ".join(f"{t.name}[{ago(t.last_run)}]" for t in tasks)], notice=True)
        if sub == "sources":
            col = {"ok": "green", "disabled": "grey", "blocked": "red", "down": "red", "degraded": "orange"}
            return Reply([f"{f.b(h.name)}: {f.c(h.status, col.get(h.status, 'grey'))}{f.sep}ok {ago(h.last_ok)}{f.sep}"
                          f"err {h.errors}/{h.requests}" + (f"{f.sep}{f.c(h.last_error[:80], 'grey')}" if h.last_error and h.status != 'ok' else "")
                          for h in app.health.all()], notice=True)
        if sub == "reload":
            app.cfg.reload()
            return Reply(["Configuration reloaded (settings/sources/scoring)."], notice=True)
        if sub == "pause":
            await app.publisher.set_paused(True)
            return Reply([f"Automatic posting {f.b(f.c('PAUSED', 'red'))} (commands still work)."])
        if sub == "resume":
            await app.publisher.set_paused(False)
            return Reply([f"Automatic posting {f.b(f.c('RESUMED', 'green'))}."])
        if sub == "alerts":
            st = await app.publisher.stats(24)
            return Reply([f"{f.b(cat)}: sent {f.c(d['sent'], 'green')}"
                          + (f", suppressed {d['suppressed']}" if d['suppressed'] else "")
                          for cat, d in sorted(st.items())] or ["No alerts in the last 24h."], notice=True)
        if sub == "logs":
            items = recent(8)
            return Reply([f"{fmt_ts(t)} {f.c(lvl, 'red' if lvl == 'ERROR' else 'orange')} {name}: {msg[:200]}"
                          for t, lvl, name, msg in items] or ["No recent warnings."], notice=True)
        if sub == "config":
            s = app.cfg.redacted()["settings"]
            nets = {n: list((v or {}).get("channels", {}).keys()) for n, v in (s.get("networks") or {}).items()}
            en = [k for sec in ("news", "market", "esports") for k, v in (app.cfg.sources.get(sec) or {}).items()
                  if isinstance(v, dict) and v.get("enabled")]
            return Reply([f"{f.k('Networks/channels:')} {nets}{f.sep}{f.k('currency')} {s.get('market', {}).get('currency')}"
                          f"{f.sep}{f.k('timezone')} {s.get('bot', {}).get('timezone')}",
                          f"{f.k('Active sources:')} {', '.join(en)}",
                          f"{f.k('Weights:')} {app.cfg.scoring.get('weights')}"], notice=True)
        if sub == "backup":
            p = await backup(app.db, app.backup_dir, int(app.cfg.get("settings.maintenance.backup_keep", 14)))
            return Reply([f"Backup created: {p.name}" if p else "Backup: use pg_dump with PostgreSQL."], notice=True)
        if sub == "refresh":
            name = parts[1] if len(parts) > 1 else ""
            if name not in app.scheduler.tasks:
                return Reply(["Tasks: " + ", ".join(sorted(app.scheduler.tasks))], notice=True)
            await app.scheduler.run_now(name)
            return Reply([f"Task {f.b(name)} executed."], notice=True)
        return Reply(["Usage: " + HELP["admin"]], notice=True)
