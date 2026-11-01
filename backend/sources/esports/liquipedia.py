"""Liquipedia (MediaWiki API) — https://liquipedia.net/api-terms-of-use

Usage rules enforced through configuration:
- Identifying User-Agent with contact details (mandatory), gzip.
- At most 1 request every 2 s; `action=parse` at most 1 every 30 s (`liquipedia_parse` profile).
- Aggressive caching: matches refresh at most every 60 s; teams/players every 24 h.
The LiquipediaDB data API (v3, keyed) is not used; if you subscribe it can be added as another adapter.
"""
from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup

from ...core.http import HttpClient
from .base import LiveMatch, MapState, bo_from, guess_tier

API = "https://liquipedia.net/counterstrike/api.php"
WIKI = "https://liquipedia.net/counterstrike/"


def _txt(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def parse_matches_html(html: str, now: float | None = None) -> list[LiveMatch]:
    now = now or time.time()
    soup = BeautifulSoup(html, "lxml")
    out: list[LiveMatch] = []
    for area in soup.select("[data-toggle-area-content]"):
        completed_area = area.get("data-toggle-area-content") == "2"
        for m in area.select(".match-info"):
            timer = m.select_one(".timer-object")
            ts = float(timer["data-timestamp"]) if timer and timer.get("data-timestamp", "").isdigit() else None
            finished = bool(timer and timer.get("data-finished")) or completed_area
            opps = m.select(".match-info-header-opponent")
            names = []
            for o in opps[:2]:
                a = o.select_one(".name a")
                names.append((a.get("title") or _txt(a)).replace(" (page does not exist)", "") if a else _txt(o) or "TBD")
            if len(names) < 2:
                continue
            holder = m.select_one(".match-info-header-scoreholder")
            upper = _txt(holder.select_one(".match-info-header-scoreholder-upper")) if holder else ""
            lower = _txt(holder.select_one(".match-info-header-scoreholder-lower")) if holder else ""
            bo = bo_from(lower) or 1
            score1 = score2 = 0
            has_score = False
            sm = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", upper)
            if sm:
                score1, score2, has_score = int(sm.group(1)), int(sm.group(2)), True
            elif re.match(r"^\s*(FF|W|L)\s*:\s*(FF|W|L)\s*$", upper):
                w1 = upper.strip().startswith("W")  # win by forfeit
                score1, score2 = (1, 0) if w1 else (0, 1)
                has_score = True
            tour = m.select_one(".match-info-tournament-name")
            tlink = tour.select_one("a") if tour else None
            event = _txt(tour)
            stage = event.split(" - ", 1)[1] if " - " in event else ""
            status = "finished" if finished else "upcoming"
            inferred = False
            if not finished and ts and ts <= now:
                status = "live"
                inferred = not has_score  # started per schedule, score not confirmed
            sid = f"{int(ts or 0)}:{names[0]}:{names[1]}"
            out.append(LiveMatch(source="liquipedia", source_id=sid, team1=names[0], team2=names[1],
                                 event=event.split(" - ")[0], stage=stage, best_of=bo, status=status, start_time=ts,
                                 score1=score1, score2=score2, tier=guess_tier(event),
                                 url=("https://liquipedia.net" + tlink["href"]) if tlink and tlink.get("href") else None,
                                 inferred=inferred))
    return out


# ------------------------------------------------------------- wikitext
def _field(wikitext: str, name: str) -> str | None:
    m = re.search(r"^\|\s*" + re.escape(name) + r"\s*=(.*)$", wikitext, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    return clean_wiki(v) or None


def clean_wiki(v: str) -> str:
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"\{\{Flag\|[^}]*\}\}\s*", "", v, flags=re.I)
    v = re.sub(r"\{\{abbr\|([^|}]*)\|[^}]*\}\}", r"\1", v, flags=re.I)
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    v = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"<br\s*/?>", ", ", v)
    v = re.sub(r"'{2,}", "", v)
    v = re.sub(r"<[^>]+>", "", v)
    return re.sub(r"\s+", " ", v).strip(" ,")


def parse_team(wikitext: str, title: str) -> dict:
    info = wikitext[wikitext.find("{{Infobox team"):] if "{{Infobox team" in wikitext else ""
    info = info[: info.find("\n}}") + 3] if info else ""
    data = {"name": _field(info, "name") or title, "location": _field(info, "location"),
            "region": _field(info, "region"), "coach": _field(info, "coaches") or _field(info, "coach"),
            "igl": _field(info, "igl"), "url": WIKI + title.replace(" ", "_")}
    roster = []
    active = re.search(r"\{\{Squad\|status=active(.*?)\n\}\}", wikitext, re.S)
    if active:
        for pm in re.finditer(r"\{\{Person\|([^\n]*)\}\}", active.group(1)):
            params = dict(re.findall(r"(\w+)=([^|}]*)", pm.group(1)))
            roster.append({"id": params.get("id", "").strip(), "name": clean_wiki(params.get("name", "")),
                           "flag": params.get("flag", "").strip(), "role": params.get("role", "").strip(),
                           "igl": params.get("igl", "").strip() == "y"})
    data["players"] = [p for p in roster if not p["role"]]
    data["staff"] = [p for p in roster if p["role"]]
    return data


def parse_player(wikitext: str, title: str) -> dict:
    info = wikitext[wikitext.find("{{Infobox player"):] if "{{Infobox player" in wikitext else ""
    end = info.find("\n|team_history")
    info = info[: end if end > 0 else info.find("\n}}") + 3]
    return {"id": _field(info, "id") or title, "name": _field(info, "romanized_name") or _field(info, "name"),
            "country": _field(info, "country"), "birth_date": _field(info, "birth_date"),
            "team": _field(info, "team"), "roles": _field(info, "roles"), "status": _field(info, "status"),
            "url": WIKI + title.replace(" ", "_")}


def parse_tournaments(wikitext: str) -> list[dict]:
    out, status = [], None
    for line in wikitext.splitlines():
        if re.match(r"^\*[^*]", line):
            label = line[1:].strip().lower()
            status = {"upcoming": "upcoming", "ongoing": "ongoing", "concluded": "concluded"}.get(label, label)
        elif line.startswith("**") and status:
            parts = line[2:].split("|")
            if len(parts) >= 2:
                out.append({"page": parts[0].strip(), "name": parts[1].strip(), "status": status,
                            "is_major": bool(re.search(r"\bmajor\b", parts[1], re.I))})
    return out


class Liquipedia:
    name = "liquipedia"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def matches(self) -> list[LiveMatch]:
        data = await self.http.get_json("liquipedia_parse", API, params={
            "action": "parse", "page": "Liquipedia:Matches", "format": "json", "prop": "text"}, cache_ttl=55)
        return parse_matches_html(data["parse"]["text"]["*"])

    async def wikitext(self, title: str) -> tuple[str, str] | None:
        data = await self.http.get_json("liquipedia", API, params={
            "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main", "format": "json",
            "titles": title, "redirects": 1}, cache_ttl=3600)
        pages = (data.get("query") or {}).get("pages") or {}
        for p in pages.values():
            if "missing" in p or not p.get("revisions"):
                return None
            return p["title"], p["revisions"][0]["slots"]["main"]["*"]
        return None

    async def search(self, text: str) -> str | None:
        data = await self.http.get_json("liquipedia", API, params={
            "action": "opensearch", "search": text, "limit": 5, "namespace": 0, "format": "json"}, cache_ttl=3600)
        return data[1][0] if isinstance(data, list) and len(data) > 1 and data[1] else None

    async def team(self, name: str) -> dict | None:
        for title in (name, await self.search(name)):
            if not title:
                continue
            wt = await self.wikitext(title)
            if wt and "{{Infobox team" in wt[1]:
                return parse_team(wt[1], wt[0])
        return None

    async def player(self, name: str) -> dict | None:
        for title in (name, await self.search(name)):
            if not title:
                continue
            wt = await self.wikitext(title)
            if wt and "{{Infobox player" in wt[1]:
                return parse_player(wt[1], wt[0])
        return None

    async def tournaments(self) -> list[dict]:
        wt = await self.wikitext("Liquipedia:Tournaments")
        return parse_tournaments(wt[1]) if wt else []
