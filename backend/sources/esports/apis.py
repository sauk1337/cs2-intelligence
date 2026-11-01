"""Competitive sources with an API: PandaScore, FACEIT Data API and Valve Regional Standings (VRS)."""
from __future__ import annotations

import re
import time
from datetime import datetime

from ...core.http import HttpClient
from .base import LiveMatch, MapState, guess_tier

PANDA = "https://api.pandascore.co"
FACEIT = "https://open.faceit.com/data/v4"
VRS_REPO = "https://api.github.com/repos/ValveSoftware/counter-strike_regional_standings/contents/live"
VRS_RAW = "https://raw.githubusercontent.com/ValveSoftware/counter-strike_regional_standings/main/live"


def _iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------- PandaScore
def parse_pandascore(rows: list) -> list[LiveMatch]:
    out = []
    for r in rows or []:
        opp = [(o.get("opponent") or {}) for o in r.get("opponents") or []]
        if len(opp) < 2:
            continue
        ids = [o.get("id") for o in opp]
        res = {x.get("team_id"): x.get("score", 0) for x in r.get("results") or []}
        status = {"running": "live", "finished": "finished", "not_started": "upcoming"}.get(r.get("status"), "upcoming")
        maps = []
        for g in sorted(r.get("games") or [], key=lambda g: g.get("position") or 0):
            gst = {"running": "live", "finished": "done", "not_started": "pending"}.get(g.get("status"), "pending")
            name = ((g.get("map") or {}).get("name")) or f"Mapa {g.get('position')}"
            winner = (g.get("winner") or {}).get("id")
            s1 = s2 = None
            if gst == "done" and winner:
                s1, s2 = (1, 0) if winner == ids[0] else (0, 1)   # PandaScore REST gives no round scores on the basic plan
            maps.append(MapState(name=name, s1=s1, s2=s2, status=gst))
        live = next((m for m in maps if m.status == "live"), None)
        event = " ".join(x for x in [(r.get("league") or {}).get("name"), (r.get("serie") or {}).get("full_name")] if x)
        out.append(LiveMatch(source="pandascore", source_id=str(r.get("id")), team1=opp[0].get("name", "TBD"),
                             team2=opp[1].get("name", "TBD"), event=event,
                             stage=(r.get("tournament") or {}).get("name", ""), best_of=r.get("number_of_games") or 1,
                             status=status, start_time=_iso(r.get("begin_at") or r.get("scheduled_at")),
                             score1=res.get(ids[0], 0) or 0, score2=res.get(ids[1], 0) or 0, maps=maps,
                             current_map=live.name if live else None, tier=guess_tier(event),
                             url=None))
    return out


class PandaScore:
    name = "pandascore"

    def __init__(self, http: HttpClient, token: str, game: str = "csgo") -> None:
        self.http, self.token, self.game = http, token, game

    async def _get(self, path: str, **params) -> list:
        return await self.http.get_json("pandascore", f"{PANDA}/{self.game}/{path}", params=params,
                                        headers={"Authorization": f"Bearer {self.token}"})

    async def matches(self) -> list[LiveMatch]:
        rows = await self._get("matches/running", per_page=50)
        rows += await self._get("matches/upcoming", per_page=30, sort="begin_at")
        return parse_pandascore(rows)

    async def results(self) -> list[LiveMatch]:
        return parse_pandascore(await self._get("matches/past", per_page=30, sort="-end_at"))


# ----------------------------------------------------------------- FACEIT
def parse_faceit_matches(data: dict) -> list[LiveMatch]:
    out = []
    for r in (data or {}).get("items", []) or []:
        teams = r.get("teams") or {}
        f1, f2 = teams.get("faction1") or {}, teams.get("faction2") or {}
        score = ((r.get("results") or {}).get("score")) or {}
        st = str(r.get("status", "")).upper()
        status = "live" if st in ("ONGOING", "VOTING", "CONFIGURING", "READY") else "finished" if st == "FINISHED" else "upcoming"
        picks = (((r.get("voting") or {}).get("map") or {}).get("pick")) or []
        maps = [MapState(name=p.replace("de_", "").title(), status="pending") for p in picks]
        out.append(LiveMatch(source="faceit", source_id=r.get("match_id", ""), team1=f1.get("name", "TBD"),
                             team2=f2.get("name", "TBD"), event=r.get("competition_name", "FACEIT"),
                             best_of=r.get("best_of") or 1, status=status,
                             start_time=r.get("started_at") or r.get("scheduled_at"),
                             score1=int(score.get("faction1") or 0), score2=int(score.get("faction2") or 0), maps=maps,
                             current_map=maps[len([m for m in maps if m.status == 'done'])].name if status == "live" and maps else None,
                             tier=2, url=(r.get("faceit_url") or "").replace("{lang}", "en")))
    return out


def parse_faceit_stats(data: dict) -> dict:
    """Player stats per map (Kills, Deaths, Assists, ADR, K/D, MVPs)."""
    rounds = (data or {}).get("rounds") or []
    maps, players = [], []
    for rd in rounds:
        rs = rd.get("round_stats") or {}
        sc = re.findall(r"\d+", rs.get("Score", ""))
        maps.append({"map": rs.get("Map", "").replace("de_", "").title(),
                     "score": f"{sc[0]}-{sc[1]}" if len(sc) >= 2 else rs.get("Score")})
        for t in rd.get("teams") or []:
            tname = (t.get("team_stats") or {}).get("Team", "")
            for p in t.get("players") or []:
                ps = p.get("player_stats") or {}
                players.append({"map": maps[-1]["map"], "team": tname, "player": p.get("nickname"),
                                "kills": ps.get("Kills"), "deaths": ps.get("Deaths"), "assists": ps.get("Assists"),
                                "adr": ps.get("ADR"), "kd": ps.get("K/D Ratio"), "mvps": ps.get("MVPs")})
    return {"maps": maps, "players": players, "scope": "per map"} if players else {}


class Faceit:
    name = "faceit"

    def __init__(self, http: HttpClient, api_key: str, championships: list, hubs: list) -> None:
        self.http, self.key = http, api_key
        self.championships, self.hubs = championships or [], hubs or []

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    async def matches(self) -> list[LiveMatch]:
        out = []
        for cid in self.championships:
            for typ in ("ongoing", "upcoming"):
                d = await self.http.get_json("faceit", f"{FACEIT}/championships/{cid}/matches",
                                             params={"type": typ, "limit": 20}, headers=self._h())
                out += parse_faceit_matches(d)
        for hid in self.hubs:
            d = await self.http.get_json("faceit", f"{FACEIT}/hubs/{hid}/matches",
                                         params={"type": "ongoing", "limit": 20}, headers=self._h())
            out += parse_faceit_matches(d)
        return out

    async def stats(self, match_id: str) -> dict:
        d = await self.http.get_json("faceit", f"{FACEIT}/matches/{match_id}/stats", headers=self._h(), cache_ttl=30)
        return parse_faceit_stats(d)


# -------------------------------------------------------------------- VRS
def parse_vrs(md: str) -> list[dict]:
    out = []
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0].isdigit():
            out.append({"position": int(cells[0]), "points": float(cells[1]) if re.match(r"^[\d.]+$", cells[1]) else None,
                        "team": cells[2], "roster": cells[3]})
    return out


class ValveStandings:
    name = "vrs"

    def __init__(self, http: HttpClient, region: str = "global") -> None:
        self.http, self.region = http, region

    async def latest(self) -> tuple[str, list[dict]]:
        year = time.gmtime().tm_year
        files = []
        for y in (year, year - 1):
            try:
                listing = await self.http.get_json("vrs", f"{VRS_REPO}/{y}", cache_ttl=3600,
                                                   headers={"Accept": "application/vnd.github+json"})
            except Exception:
                continue
            files = sorted(f["name"] for f in listing if f.get("name", "").startswith(f"standings_{self.region}_"))
            if files:
                break
        if not files:
            return "", []
        latest = files[-1]
        year_dir = re.search(r"_(\d{4})_", latest).group(1)
        r = await self.http.get("vrs", f"{VRS_RAW}/{year_dir}/{latest}", cache_ttl=3600)
        edition = latest.removeprefix(f"standings_{self.region}_").removesuffix(".md").replace("_", "-")
        return edition, parse_vrs(r.text)
