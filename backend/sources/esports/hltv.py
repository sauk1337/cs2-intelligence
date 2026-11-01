"""HLTV.org — ONLY with access authorised by HLTV (server IP and/or an agreed header/cookie).

HLTV has no public API and its website is protected by Cloudflare. Without authorisation it returns 403;
in that case the source is paused (cooldown) and the next configured source is used. No attempt is made
to bypass any protection. Selectors target the 2025-2026 layout with older fallbacks;
if HLTV changes its HTML, `python -m backend.main check` reports it.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ...core.http import HttpClient
from .base import LiveMatch, MapState, bo_from, guess_tier

BASE = "https://www.hltv.org"


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _id(href: str | None) -> str | None:
    m = re.search(r"/matches/(\d+)/", href or "")
    return m.group(1) if m else None


def _unix(el) -> float | None:
    if not el:
        return None
    for k in ("data-unix", "data-zonedgrouping-entry-unix"):
        v = el.get(k)
        if v and v.isdigit():
            return int(v) / 1000 if int(v) > 1e11 else float(v)
    return None


def parse_match_list(html: str) -> list[LiveMatch]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, LiveMatch] = {}
    for c in soup.select(".match-wrapper, .liveMatch-container, .upcomingMatch"):
        link = c if c.name == "a" and (c.get("href") or "").startswith("/matches/") else c.select_one('a[href^="/matches/"]')
        href = link.get("href") if link else None
        mid = c.get("data-match-id") or _id(href)
        if not mid or mid in out:
            continue
        names = [_txt(e) for e in c.select(".match-teamname, .matchTeamName")] or [_txt(e) for e in c.select(".team")]
        live = (c.get("live") == "true" or "liveMatch-container" in (c.get("class") or [])
                or bool(c.select_one(".match-meta-live, .matchMeta.live")))
        event = c.get("data-event-headline") or _txt(c.select_one(".match-event .text-ellipsis, .matchEventName, .match-event"))
        meta = " ".join(_txt(e) for e in c.select(".match-meta, .matchMeta"))
        ts = _unix(c.select_one("[data-unix]")) or _unix(c)
        out[mid] = LiveMatch(source="hltv", source_id=mid, team1=names[0] if names else "TBD",
                             team2=names[1] if len(names) > 1 else "TBD", event=event or "", best_of=bo_from(meta) or 1,
                             status="live" if live else "upcoming", start_time=ts, tier=guess_tier(event or ""),
                             url=BASE + href if href else None)
    if not out and len(html) > 20000 and "match" in html:
        raise ValueError("HLTV /matches: unrecognised page layout")
    return list(out.values())


def parse_match_page(html: str, base: LiveMatch) -> LiveMatch:
    soup = BeautifulSoup(html, "lxml")
    teams = [_txt(e) for e in soup.select(".teamsBox .teamName")]
    maps = []
    for mh in soup.select(".mapholder"):
        name = _txt(mh.select_one(".mapname")) or "TBA"
        scores = [_txt(e) for e in mh.select(".results-team-score")]
        nums = [int(s) if s.isdigit() else None for s in scores[:2]] + [None, None]
        won1 = bool(mh.select_one(".results-left.won"))
        won2 = bool(mh.select_one(".results-right.won"))
        played = nums[0] is not None and nums[1] is not None
        status = "done" if (won1 or won2) else ("live" if played else "pending")
        maps.append(MapState(name=name if name.upper() != "TBA" else "Por decidir", s1=nums[0], s2=nums[1], status=status))
    w1 = sum(1 for m in maps if m.status == "done" and (m.s1 or 0) > (m.s2 or 0))
    w2 = sum(1 for m in maps if m.status == "done" and (m.s2 or 0) > (m.s1 or 0))
    fmt = _txt(soup.select_one(".preformatted-text"))
    bo = bo_from(fmt) or base.best_of or len(maps) or 1
    status = base.status
    countdown = _txt(soup.select_one(".countdown")).lower()
    if "match over" in countdown or max(w1, w2) > bo / 2:
        status = "finished"
    elif "live" in countdown:
        status = "live"
    live_map = next((m for m in maps if m.status == "live"), None)
    if status == "live" and not live_map:
        nxt = next((m for m in maps if m.status == "pending"), None)
        current = nxt.name if nxt else None
    else:
        current = live_map.name if live_map else None
    base.team1, base.team2 = (teams + [base.team1, base.team2])[0:2] if len(teams) >= 2 else (base.team1, base.team2)
    base.maps, base.score1, base.score2, base.best_of, base.status, base.current_map = maps, w1, w2, bo, status, current
    base.has_live_score = bool(live_map)
    event = _txt(soup.select_one(".timeAndEvent .event a"))
    if event:
        base.event = event
        base.tier = guess_tier(event)
    stats = parse_stats(soup)
    if stats:
        base.stats = {"players": stats, "scope": "series (all maps played)"}
    return base


def parse_stats(soup: BeautifulSoup) -> list[dict]:
    out = []
    box = soup.select_one("#all-content") or soup.select_one(".stats-content")
    if not box:
        return out
    for table in box.select("table.totalstats"):
        team = _txt(table.select_one(".teamName, .team-name, tr.header-row td"))
        for tr in table.select("tr")[1:]:
            nick = _txt(tr.select_one(".player-nick")) or _txt(tr.select_one(".players .text-ellipsis"))
            if not nick:
                continue
            kd = _txt(tr.select_one("td.kd"))
            k, d = (kd.split("-") + ["", ""])[:2]
            out.append({"team": team, "player": nick, "kills": k.strip(), "deaths": d.strip(),
                        "adr": _txt(tr.select_one("td.adr")), "kast": _txt(tr.select_one("td.kast")),
                        "rating": _txt(tr.select_one("td.rating"))})
    return out


def parse_results(html: str) -> list[LiveMatch]:
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for c in soup.select(".result-con"):
        a = c.select_one('a[href^="/matches/"]') or c.find_parent("a")
        href = a.get("href") if a else None
        mid = _id(href)
        if not mid or mid in out:
            continue
        t = [_txt(e) for e in c.select(".team")]
        sc = [int(_txt(e)) for e in c.select(".result-score span") if _txt(e).isdigit()]
        map_text = _txt(c.select_one(".map-text"))
        bo = bo_from(map_text) or 1
        s1, s2 = (sc + [0, 0])[:2]
        maps = []
        if bo == 1 and s1 + s2 > 1:   # in a BO1, HLTV shows the rounds of the map
            maps = [MapState(name=map_text, s1=s1, s2=s2, status="done")]
            s1, s2 = (1, 0) if s1 > s2 else (0, 1)
        ev = _txt(c.select_one(".event-name"))
        out[mid] = LiveMatch(source="hltv", source_id=mid, team1=t[0] if t else "TBD", team2=t[1] if len(t) > 1 else "TBD",
                             event=ev, best_of=bo, status="finished", score1=s1, score2=s2, maps=maps,
                             start_time=_unix(c) or _unix(c.find_parent(attrs={"data-zonedgrouping-entry-unix": True})),
                             tier=guess_tier(ev), url=BASE + href if href else None)
    return list(out.values())


class Hltv:
    name = "hltv"

    def __init__(self, http: HttpClient, detail_limit: int = 6) -> None:
        self.http = http
        self.detail_limit = detail_limit

    async def matches(self) -> list[LiveMatch]:
        r = await self.http.get("hltv", BASE + "/matches", cache_ttl=8)
        items = parse_match_list(r.text)
        for m in [m for m in items if m.status == "live" and m.url][: self.detail_limit]:
            page = await self.http.get("hltv", m.url, cache_ttl=8)
            parse_match_page(page.text, m)
        return items

    async def results(self) -> list[LiveMatch]:
        r = await self.http.get("hltv", BASE + "/results", cache_ttl=50)
        return parse_results(r.text)[:40]

    async def match_detail(self, m: LiveMatch) -> LiveMatch:
        page = await self.http.get("hltv", m.url, cache_ttl=8)
        return parse_match_page(page.text, m)
