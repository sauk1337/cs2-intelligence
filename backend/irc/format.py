"""IRC message formatting: compact, readable, colourful (mIRC codes) and consistent.

Style guide
- Every message starts with a coloured badge:  ▌CS2 LIVE▐  ▌SKIN OPPORTUNITY▐ …
- Field names in grey, values in bold. Rises in green, drops in red.
- Labels/risk/liquidity/trend have fixed colours so they can be read at a glance.
- URLs are always plain text at the end (formatting codes next to a URL break some clients).
- `irc.colors: false` in settings.yaml produces the same text with no codes at all.
"""
from __future__ import annotations

import time

from ..analytics.scoring import COMPONENT_NAMES
from ..news.classify import CATEGORIES
from ..utils.text import ago, badge, bold, color, fmt_dt, money, pct, signed_color, until

BADGES = {  # key: (text, fg, bg)
    "news": ("CS2 NEWS", "white", "blue"),
    "valve": ("VALVE UPDATE", "white", "orange"),
    "alert": ("MARKET ALERT", "black", "yellow"),
    "opp": ("SKIN OPPORTUNITY", "black", "lgreen"),
    "trend": ("MARKET TREND", "white", "purple"),
    "market": ("CS2 MARKET", "white", "grey"),
    "arb": ("ARBITRAGE", "white", "green"),
    "esports": ("ESPORTS", "white", "lblue"),
    "live": ("CS2 LIVE", "white", "red"),
    "result": ("RESULT", "black", "lgreen"),
    "watch": ("WATCHLIST", "black", "yellow"),
}
LABEL_STYLE = {"EXCELLENT": ("★ EXCELLENT", "lgreen"), "INTERESTING": ("INTERESTING", "green"),
               "WATCH": ("WATCH", "orange"), "HIGH RISK": ("⚠ HIGH RISK", "red"), "DISCARDED": ("DISCARDED", "grey")}
LEVEL_COLORS = {"LOW": "green", "MEDIUM": "orange", "HIGH": "red"}          # risk
LIQ_COLORS = {"HIGH": "green", "MEDIUM": "orange", "LOW": "red"}            # liquidity (higher is better)
TREND_STYLE = {"BULLISH": ("▲ BULLISH", "green"), "BEARISH": ("▼ BEARISH", "red"), "STABLE": ("► STABLE", "grey")}
VOL_STYLE = {"RISING": ("▲ RISING", "green"), "FALLING": ("▼ FALLING", "red"), "STABLE": ("► STABLE", "grey")}
CATEGORY_COLORS = {"torneos": "lblue", "equipos": "cyan", "jugadores": "cyan", "fichajes": "pink",
                   "actualizacion": "orange", "parche": "orange", "mapas": "orange", "economia": "green",
                   "cajas": "green", "operacion": "orange", "mercado": "green", "escena": "lblue"}
SOURCE_NAMES = {"skinport": "Skinport", "steam": "Steam", "csfloat": "CSFloat", "buff163": "Buff163",
                "pricempire": "Pricempire", "hltv": "HLTV", "liquipedia": "Liquipedia", "pandascore": "PandaScore",
                "faceit": "FACEIT", "hltv_rss": "HLTV", "dust2_rss": "Dust2.us", "steam_cs2": "Steam (Valve)",
                "dexerto": "Dexerto", "esports_insider": "Esports Insider", "pley": "Pley.gg",
                "reddit_globaloffensive": "r/GlobalOffensive"}


class Formatter:
    def __init__(self, colors: bool = True, tz: str = "Europe/Madrid", disclaimer: str = "") -> None:
        self.colors = colors
        self.tz = tz
        self.disclaimer = disclaimer

    # ------------------------------------------------------------ helpers
    def tag(self, key: str) -> str:
        text, fg, bg = BADGES[key]
        return badge(text, fg, bg, self.colors)

    @staticmethod
    def src(name: str) -> str:
        return SOURCE_NAMES.get(name, name)

    def c(self, text: str, col: str) -> str:
        return color(str(text), col, self.colors)

    def b(self, text) -> str:
        return bold(str(text), self.colors)

    def k(self, key: str) -> str:
        """Field name (grey)."""
        return self.c(key, "grey")

    @property
    def sep(self) -> str:
        return self.c(" · ", "grey")

    def link(self, url: str | None) -> str:
        return f"{self.c('»', 'grey')} {url}" if url else ""

    def price(self, value, currency: str = "EUR") -> str:
        return self.b(money(value, currency))

    def chg(self, value, invert: bool = False) -> str:
        return signed_color(value, enabled=self.colors, invert=invert)

    def label(self, lab: str) -> str:
        text, col = LABEL_STYLE.get(lab, (lab, "grey"))
        return self.b(self.c(text, col))

    def risk(self, r: str) -> str:
        return self.b(self.c(r, LEVEL_COLORS.get(r, "grey")))

    def liq(self, lab: str) -> str:
        return self.c(lab, LIQ_COLORS.get(lab, "grey"))

    def trend(self, lab: str) -> str:
        text, col = TREND_STYLE.get(lab, (lab, "grey"))
        return self.c(text, col)

    def vol(self, lab: str) -> str:
        text, col = VOL_STYLE.get(lab, (lab, "grey"))
        return self.c(text, col)

    def score(self, s: float) -> str:
        col = "lgreen" if s >= 80 else "green" if s >= 65 else "orange" if s >= 50 else "red"
        return self.b(self.c(f"{s:.0f}/100", col))

    def team(self, name: str, winner: bool = False) -> str:
        return self.b(self.c(name, "green")) if winner else self.b(name)

    # ------------------------------------------------------------ news
    def category(self, cat: str) -> str:
        return self.b(self.c(CATEGORIES.get(cat, cat.upper()), CATEGORY_COLORS.get(cat, "lblue")))

    def news(self, n, with_link: bool = True) -> list[str]:
        title = n.title_es or n.title
        hot = self.c(" ⚡", "yellow") if n.important else ""
        valve = n.category in ("actualizacion", "parche") and "steam" in n.source
        lines = [f"{self.tag('valve' if valve else 'news')} {self.category(n.category)}{hot} {self.b(title)}"]
        summary = n.summary_es or n.summary or ""
        if summary and summary.strip().lower() != title.strip().lower():
            lines.append(self.c("» ", "grey") + summary[:260])
        if with_link:
            lines.append(f"{self.k('Source:')} {self.src(n.source)}{self.sep}{self.k(fmt_dt(n.published_at, self.tz))}"
                         f"{self.sep}{n.url}")
        return lines

    def news_compact(self, n) -> str:
        hot = self.c("⚡", "yellow") + " " if n.important else ""
        return (f"{self.category(n.category)} {hot}{self.b((n.title_es or n.title)[:140])} "
                f"{self.c('— ' + self.src(n.source) + ', ' + ago(n.published_at), 'grey')} {self.link(n.url)}")

    def valve_update(self, n, analysis) -> list[str]:
        lines = [f"{self.tag('valve')} {self.b(n.title_es or n.title)} {self.c('(' + fmt_dt(n.published_at, self.tz) + ')', 'grey')}"]
        if analysis.facts:
            lines.append(f"{self.b(self.c('FACTS:', 'lblue'))} " + self.c(" | ", "grey").join(f[:110] for f in analysis.facts[:3]))
        if analysis.impacts:
            lines.append(f"{self.b(self.c('POSSIBLE MARKET IMPACT', 'orange'))} {self.c('(speculation, not confirmed):', 'grey')} "
                         + "; ".join(analysis.impacts[:3]))
        lines.append(f"{self.k('Full patch notes:')} {n.url}")
        return lines

    # ------------------------------------------------------------ market
    def change_str(self, m, keys=("24h", "7d", "30d")) -> str:
        parts = [f"{self.k(k)} {self.chg(m.change[k][0])}" for k in keys if k in m.change]
        if not parts:
            return self.c("no measurable change yet", "grey")
        bases = {m.change[k][1].startswith("vs median") for k in keys if k in m.change}
        note = "vs median sale of each period" if bases == {True} else \
            "own price history" if bases == {False} else "own history + median sales"
        return " ".join(parts) + " " + self.c(f"({note})", "grey")

    def opportunity(self, ev) -> list[str]:
        m, sc = ev.metrics, ev.score
        others = self.sep.join(f"{self.src(s)} {money(p, m.currency)}" for s, p, _u, _a in m.platforms if s != m.source)
        return [
            f"{self.tag('opp')} {self.label(sc.label)} {self.b(m.short)} {self.c('—', 'grey')} {self.price(m.price, m.currency)}"
            f"{self.sep}{self.k('Score')} {self.score(sc.score)}{self.sep}{self.k('Risk')} {self.risk(sc.risk)}",
            f"{self.k('Change:')} {self.change_str(m)}{self.sep}{self.k('30d median')} {self.b(money(m.median30, m.currency))}"
            f"{self.sep}{self.k('90d low')} {money(m.min90, m.currency)}",
            f"{self.k('Volume')} {self.vol(m.volume_label)} ({m.volume_30d or 0} sales/30d){self.sep}"
            f"{self.k('Liquidity')} {self.liq(m.liquidity_label)}{self.sep}{self.k('Trend')} {self.trend(m.trend_label)}",
            f"{self.k('Why:')} {sc.reason}",
            f"{self.k('Source:')} {self.src(m.source)}" + (f"{self.sep}{self.k('Others:')} {others}" if others else "")
            + (f"{self.sep}{self.c(self.disclaimer, 'grey')}" if self.disclaimer else "") + f"  {self.link(m.url)}",
        ]

    def arbitrage(self, ev, a) -> list[str]:
        m = ev.metrics
        return [
            f"{self.tag('arb')} {self.label(a.label)} {self.b(m.short)}: {self.k('buy on')} {self.b(self.src(a.buy_source))} "
            f"{self.price(a.buy_price, m.currency)} {self.c('→', 'grey')} {self.k('sell on')} {self.b(self.src(a.sell_source))} "
            f"~{self.price(a.sell_price, m.currency)}",
            f"{self.k('Gross')} {self.chg(a.gross_pct)}{self.sep}{self.k('Est. net after fees')} {self.b(self.chg(a.profit_pct))} "
            f"({money(a.profit, m.currency)}){self.sep}{self.k('Liquidity')} {self.liq(m.liquidity_label)}",
            f"{self.c('⚠', 'orange')} " + ("; ".join(a.warnings) if a.warnings else "no warnings")
            + self.c(" · not a guaranteed profit", "grey"),
            f"{self.link(a.buy_url)}",
        ]

    def skin(self, ev, variants: list = ()) -> list[str]:
        m = ev.metrics
        meta = [x for x in (ev.item.collection, ev.item.rarity) if x]
        plats = self.sep.join(f"{self.src(s)} {self.b(money(p, m.currency))}" for s, p, _u, _a in m.platforms[:4])
        rng = f"{money(m.min90, m.currency)}–{money(m.max90, m.currency)}"
        if m.max90 and m.median30 and m.max90 > 4 * m.median30:
            rng += " " + self.c("(max is an isolated outlier sale)", "grey")
        lines = [
            f"{self.tag('market')} {self.b(ev.item.key)}" + (" " + self.c("(" + ", ".join(meta) + ")", "grey") if meta else ""),
            f"{self.k('Price')} {self.price(m.price, m.currency)} {self.c('(' + self.src(m.source) + ', ' + ago(m.price_ts) + ')', 'grey')}"
            + (f"{self.sep}{plats}" if len(m.platforms) > 1 else ""),
            f"{self.k('Change:')} {self.change_str(m)}",
            f"{self.k('30d median')} {self.b(money(m.median30, m.currency))}{self.sep}{self.k('90d range')} {rng}",
            f"{self.k('Volume')} {m.volume_24h if m.volume_24h is not None else 'n/a'} (24h) / {m.volume_7d or 'n/a'} (7d) "
            f"{self.vol(m.volume_label)}{self.sep}{self.k('Liquidity')} {self.liq(m.liquidity_label)}{self.sep}"
            f"{self.k('Trend')} {self.trend(m.trend_label)}"
            + (f"{self.sep}{self.k('Bid/ask spread')} {m.spread_pct:.1f}%" if m.spread_pct is not None
               and m.spread_basis.startswith("ask/bid") else ""),
        ]
        if variants:
            lines.append(f"{self.k('Variants:')} " + ", ".join(variants[:6]))
        if m.stale:
            lines.append(self.c("⚠ Price has not been updated recently.", "orange"))
        return lines

    def score_breakdown(self, ev) -> list[str]:
        sc = ev.score
        comps = self.sep.join(f"{COMPONENT_NAMES[c.key]} {self.b(f'{c.points:.0f}')}/{c.max_points:.0f}" for c in sc.components)
        lines = [f"{self.k('Score')} {self.score(sc.score)} {self.label(sc.label)}{self.sep}{self.k('Risk')} {self.risk(sc.risk)}",
                 f"{comps}{self.sep}{self.k('Manipulation penalty')} {self.c(f'-{sc.penalty:.0f}', 'red')}"]
        lines.append(f"{self.k('Details:')} " + self.c(" | ", "grey").join(
            f"{self.b(COMPONENT_NAMES[c.key])}: {c.explanation}" for c in sc.components[:3]))
        if sc.penalty_notes:
            lines.append(f"{self.c('⚠ Risks:', 'orange')} " + "; ".join(sc.penalty_notes))
        if sc.break_even_pct:
            lines.append(self.c(f"To break even reselling on {self.src(ev.metrics.source)} the price must rise "
                                f"≥{sc.break_even_pct:.1f}% (fees). Indicators, not predictions.", "grey"))
        return lines

    def quick_row(self, r: dict, field: str, currency: str) -> str:
        val = r.get(field)
        return f"{self.b(r['ref'].parsed.short)} {money(r['price'], currency)} ({self.chg(val) if val is not None else 'n/a'})"

    # ------------------------------------------------------------ esports
    def _status(self, m: dict) -> str:
        if m["status"] == "live":
            return {"stale": self.c("● LIVE (stale data)", "orange"),
                    "inferred": self.c("● PROBABLY LIVE (unconfirmed)", "orange"),
                    "conflict": self.c("● LIVE (sources disagree)", "orange")}.get(m["sync_status"],
                                                                                self.b(self.c("● LIVE", "red")))
        return {"upcoming": self.c("UPCOMING", "lblue"), "finished": self.c("FINISHED", "grey")}.get(m["status"], m["status"])

    def map_score(self, m: dict) -> str:
        live = next((x for x in m.get("maps") or [] if x.get("status") == "live" and x.get("s1") is not None), None)
        return f"{live['s1']}-{live['s2']}" if live else ""

    def series(self, m: dict) -> str:
        s1, s2 = m["score1"], m["score2"]
        if not self.colors or s1 == s2:  # level: no colour (white would vanish on light themes)
            return self.b(f"{s1}-{s2}")
        c1, c2 = ("green", "red") if s1 > s2 else ("red", "green")
        return self.b(self.c(s1, c1) + "-" + self.c(s2, c2))

    def live_line(self, m: dict) -> str:
        cur = m.get("current_map")
        ms = self.map_score(m)
        mp = f"{self.sep}{self.k('Map')} {self.c(cur, 'cyan')}{' ' + self.b(ms) if ms else ''}" if cur else ""
        return (f"{self.tag('live')} {self.team(m['team1'])} {self.series(m)} {self.team(m['team2'])} "
                f"{self.c('(BO' + str(m['best_of']) + ')', 'grey')}{mp}{self.sep}{m['event'][:50]}{self.sep}"
                f"{self._status(m)} {self.c(self.src(m['source']) + ' ' + ago(m['updated_at']), 'grey')}")

    def match_full(self, m: dict) -> list[str]:
        lines = [f"{self.tag('esports')} {self.team(m['team1'])} {self.c('vs', 'grey')} {self.team(m['team2'])}{self.sep}"
                 f"{self.k('Event')} {self.b(m['event'])}{' (' + m['stage'] + ')' if m.get('stage') else ''}{self.sep}"
                 f"BO{m['best_of']}"]
        when = fmt_dt(m.get("start_time"), self.tz) if m.get("start_time") else "?"
        rel = until(m.get("start_time")) if m["status"] == "upcoming" else ago(m.get("start_time"))
        lines.append(f"{self.k('Status')} {self._status(m)}{self.sep}{self.k('Series')} {self.series(m)}{self.sep}"
                     f"{self.k('Start')} {when} {self.c('(' + rel + ')', 'grey')}")
        maps = m.get("maps") or []
        if maps:
            parts = []
            for x in maps:
                s1 = x["s1"] if x.get("s1") is not None else "-"
                s2 = x["s2"] if x.get("s2") is not None else "-"
                live = self.c(" (live)", "red") if x.get("status") == "live" else ""
                parts.append(f"{self.c(x['name'], 'cyan')} {self.b(f'{s1}:{s2}')}{live}")
            lines.append(f"{self.k('Maps:')} " + self.c(" | ", "grey").join(parts))
        srcs = ", ".join(self.src(s) for s in (m.get("source_ids") or {}))
        lines.append(self.c(f"Source: {self.src(m['source'])} (confidence {m['confidence']:.2f}, {m['sync_status']}, "
                            f"updated {ago(m['updated_at'])}) · All sources: {srcs} · ID {m['key']}", "grey")
                     + (f"  {self.link(m['url'])}" if m.get("url") else ""))
        return lines

    def event(self, e) -> list[str]:
        m = e.match
        bo = self.c(f"(BO{m['best_of']})", "grey")
        if e.kind == "match_start":
            return [f"{self.tag('live')} {self.b(self.c('Match starting:', 'red'))} {self.team(m['team1'])} "
                    f"{self.c('vs', 'grey')} {self.team(m['team2'])} {bo}{self.sep}{m['event'][:60]}"
                    f"{' · ' + m['stage'] if m.get('stage') else ''}{self.sep}{self.c(self.src(m['source']), 'grey')}"]
        if e.kind == "map_change":
            return [f"{self.tag('live')} {self.team(m['team1'])} {self.c('vs', 'grey')} {self.team(m['team2'])}: "
                    f"{self.k('next map')} {self.b(self.c(e.map_name, 'cyan'))}{self.sep}{self.k('Series')} "
                    f"{self.series(m)}{self.sep}{self.c(self.src(m['source']), 'grey')}"]
        if e.kind == "map_end":
            a, _, b_ = e.detail.partition("-")
            w1 = a.isdigit() and b_.isdigit() and int(a) > int(b_)
            return [f"{self.tag('live')} {self.k('Map over:')} {self.c(e.map_name, 'cyan')} {self.team(m['team1'], w1)} "
                    f"{self.b(e.detail.replace('-', ' - '))} {self.team(m['team2'], not w1)}{self.sep}{self.k('Series')} "
                    f"{self.series(m)} {bo}{self.sep}{self.c(self.src(m['source']), 'grey')}"]
        if e.kind == "match_end":
            w1 = m["score1"] > m["score2"]
            win = m["team1"] if w1 else m["team2"]
            return [f"{self.tag('result')} {self.team(m['team1'], w1)} {self.series(m)} {self.team(m['team2'], not w1)}"
                    f"{self.sep}{self.b(self.c('🏆 ' + win + ' win', 'green'))}{self.sep}{m['event'][:60]}{self.sep}"
                    f"{self.c(self.src(m['source']), 'grey')}"]
        return [self.live_line(m)]

    def upcoming_line(self, m: dict) -> str:
        return (f"{self.c(fmt_dt(m.get('start_time'), self.tz), 'lblue')} {self.b(m['team1'])} {self.c('vs', 'grey')} "
                f"{self.b(m['team2'])} {self.c('(BO' + str(m['best_of']) + ')', 'grey')}{self.sep}{m['event'][:45]}"
                f"{self.sep}{self.c(until(m.get('start_time')), 'grey')}")

    def finished_line(self, m: dict) -> str:
        w1 = m["score1"] > m["score2"]
        return (f"{self.team(m['team1'], w1)} {self.series(m)} {self.team(m['team2'], not w1 and m['score1'] != m['score2'])}"
                f"{self.sep}{m['event'][:45]}{self.sep}"
                f"{self.c(ago(m.get('finished_at') or m.get('start_time')) + ' (' + self.src(m['source']) + ')', 'grey')}")

    @staticmethod
    def now() -> float:
        return time.time()
