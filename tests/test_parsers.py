"""Source parsers. Files in tests/fixtures are REAL responses captured on 2026-11-01;
JSON/HTML built inside the tests is SYNTHETIC (each API's documented format)."""
import json
import time

from conftest import fixture_text

from backend.news.classify import classify
from backend.sources.esports.apis import parse_faceit_matches, parse_faceit_stats, parse_pandascore, parse_vrs
from backend.sources.esports.hltv import parse_match_list, parse_match_page, parse_results
from backend.sources.esports.liquipedia import parse_matches_html, parse_player, parse_team, parse_tournaments
from backend.sources.market.buff import Buff163
from backend.sources.market.csfloat import CSFloat
from backend.sources.market.fx import FxRates
from backend.sources.market.pricempire import Pricempire
from backend.sources.market.skinport import Skinport
from backend.sources.market.steam import SteamMarket
from backend.sources.news.feeds import parse_feed, parse_steam_news


# ------------------------------------------------------------------- real
def test_liquipedia_matches_real():
    html = json.loads(fixture_text("liquipedia_matches.json"))["parse"]["text"]["*"]
    ms = parse_matches_html(html, now=1793550000)  # capture time
    up = [m for m in ms if m.status == "upcoming"]
    fin = [m for m in ms if m.status == "finished"]
    assert len(up) >= 20 and len(fin) >= 40
    m = next(m for m in fin if m.team1 == "HOTU")
    assert (m.score1, m.score2, m.best_of) == (0, 2, 3) and "Moscow Cyber Games" in m.event
    u = up[0]
    assert u.team1 == "Wildcard" and u.team2 == "BARCA esports" and u.best_of == 3 and u.stage == "Playoffs"


def test_liquipedia_inferred_live_has_no_score():
    html = json.loads(fixture_text("liquipedia_matches.json"))["parse"]["text"]["*"]
    ms = parse_matches_html(html, now=1793552400 + 60)  # 1 min after the scheduled start
    live = [m for m in ms if m.status == "live"]
    assert live and all(m.inferred for m in live)  # no score → not confirmed


def test_liquipedia_team_player_tournaments_real():
    d = json.loads(fixture_text("liquipedia_team.json"))
    p = list(d["query"]["pages"].values())[0]
    team = parse_team(p["revisions"][0]["slots"]["main"]["*"], p["title"])
    assert team["name"] == "Team Vitality"
    assert {x["id"] for x in team["players"]} == {"apEX", "ZywOo", "flameZ", "mezii", "ropz"}
    assert next(x for x in team["players"] if x["id"] == "apEX")["igl"]
    d = json.loads(fixture_text("liquipedia_player.json"))
    p = list(d["query"]["pages"].values())[0]
    pl = parse_player(p["revisions"][0]["slots"]["main"]["*"], p["title"])
    assert pl["team"] == "Team Spirit" and pl["country"] == "Russia" and pl["name"] == "Danil Kryshkovets"
    d = json.loads(fixture_text("liquipedia_tournaments.json"))
    p = list(d["query"]["pages"].values())[0]
    ts = parse_tournaments(p["revisions"][0]["slots"]["main"]["*"])
    assert any(t["is_major"] and t["status"] == "upcoming" for t in ts)


def test_vrs_real():
    rows = parse_vrs(fixture_text("vrs_global.md"))
    assert rows[0] == {"position": 1, "points": 2046.0, "team": "Spirit",
                       "roster": "donk, magixx, sh1ro, tN1R, zont1x"}
    assert len(rows) > 50


def test_skinport_history_real():
    st = Skinport.parse_history(json.loads(fixture_text("skinport_history.json")))[0]
    assert st.market_hash_name == "AK-47 | Redline (Field-Tested)"
    assert st.windows["30d"]["median"] == 23.84 and st.windows["7d"]["volume"] == 58


def test_steam_real():
    q = SteamMarket.parse_priceoverview("AK-47 | Redline (Field-Tested)",
                                        json.loads(fixture_text("steam_priceoverview.json")), "EUR")
    assert q.price == 28.37 and q.median == 28.11 and q.volume_24h == 68


def test_news_feeds_real():
    items = parse_feed("hltv_rss", fixture_text("hltv_rss.xml"))
    assert items and all(i.url.startswith("https://www.hltv.org/news/") for i in items)
    sn = parse_steam_news("steam_cs2", json.loads(fixture_text("steam_news.json")))
    assert sn and sn[0].official and "Counter-Strike 2 Update" in sn[0].title
    assert classify(sn[0].title, sn[0].summary, official=True)[0] == "actualizacion"


# -------------------------------------------------------------- synthetic
def test_skinport_items_synthetic():
    qs = Skinport.parse_items([{"market_hash_name": "X", "currency": "EUR", "min_price": 1.5, "median_price": 1.7,
                                "quantity": 3, "item_page": "u", "version": "Phase 2"}])
    assert qs[0].price == 1.5 and qs[0].version == "Phase 2"


def test_csfloat_synthetic():
    data = {"data": [{"id": "1", "price": 1520, "item": {"market_hash_name": "N"}},
                     {"id": "2", "price": 1700, "item": {"market_hash_name": "N"}}]}
    q = CSFloat.parse_listings("N", data)
    assert q.price == 15.20 and q.currency == "USD" and q.url.endswith("/item/1")


def test_buff_synthetic():
    q = Buff163.parse_goods({"code": "OK", "data": {"items": [
        {"id": 33, "market_hash_name": "N", "sell_min_price": "120.5", "buy_max_price": "115", "sell_num": 40}]}})[0]
    assert (q.price, q.bid, q.currency) == (120.5, 115.0, "CNY")
    assert Buff163.parse_goods({"code": "Login Required"}) == []


def test_pricempire_both_shapes_synthetic():
    a = Pricempire.parse({"N": {"buff163": {"price": 1234, "count": 5}}}, "buff163", "USD")
    b = Pricempire.parse([{"market_hash_name": "N", "prices": [{"provider_key": "buff163", "price": 1234}]}],
                         "buff163", "USD")
    assert a[0].price == b[0].price == 12.34


def test_fx_parse_and_convert():
    rates, date = FxRates.parse("<Cube time='2026-10-30'><Cube currency='USD' rate='1.10'/><Cube currency='CNY' rate='7.8'/></Cube>")
    assert date == "2026-10-30" and rates["USD"] == 1.10
    fx = FxRates(http=None, base="EUR")
    fx.rates = {"EUR": 1.0, **rates}
    assert abs(fx.convert(11.0, "USD") - 10.0) < 1e-9
    assert fx.convert(5, "GBP") is None  # no rate: never invented


def test_pandascore_synthetic():
    rows = [{"id": 9, "status": "running", "number_of_games": 3, "begin_at": "2026-11-01T18:00:00Z",
             "opponents": [{"opponent": {"id": 1, "name": "Vitality"}}, {"opponent": {"id": 2, "name": "Spirit"}}],
             "results": [{"team_id": 1, "score": 1}, {"team_id": 2, "score": 0}],
             "league": {"name": "IEM"}, "serie": {"full_name": "Beijing 2026"},
             "games": [{"position": 1, "status": "finished", "winner": {"id": 1}, "map": {"name": "Mirage"}},
                       {"position": 2, "status": "running", "map": {"name": "Inferno"}}]}]
    m = parse_pandascore(rows)[0]
    assert m.status == "live" and (m.score1, m.score2) == (1, 0) and m.current_map == "Inferno" and m.tier == 1


def test_faceit_synthetic():
    m = parse_faceit_matches({"items": [{"match_id": "abc", "status": "ONGOING", "best_of": 3,
                                         "teams": {"faction1": {"name": "A"}, "faction2": {"name": "B"}},
                                         "results": {"score": {"faction1": 1, "faction2": 0}},
                                         "voting": {"map": {"pick": ["de_mirage", "de_nuke"]}},
                                         "competition_name": "ESEA"}]})[0]
    assert m.status == "live" and m.maps[0].name == "Mirage"
    st = parse_faceit_stats({"rounds": [{"round_stats": {"Map": "de_nuke", "Score": "13 / 9"}, "teams": [
        {"team_stats": {"Team": "A"}, "players": [{"nickname": "p1", "player_stats": {"Kills": "20", "Deaths": "10",
                                                                                       "ADR": "95.1"}}]}]}]})
    assert st["players"][0]["kills"] == "20" and st["maps"][0]["score"] == "13-9"


HLTV_LIST = """<html><body>""" + "x" * 100 + """
<div class="match-wrapper" data-match-id="2380001" live="true" data-event-headline="IEM Beijing 2026">
  <a href="/matches/2380001/vitality-vs-spirit-iem-beijing-2026"><div class="match-teamname">Vitality</div>
  <div class="match-teamname">Spirit</div><div class="match-meta">bo3</div></a></div>
<div class="match-wrapper" data-match-id="2380002" live="false">
  <a href="/matches/2380002/a-vs-b"><div class="match-teamname">G2</div><div class="match-teamname">MOUZ</div>
  <div class="match-time" data-unix="1793600000000"></div><div class="match-meta">bo1</div></a></div>
</body></html>"""

HLTV_PAGE = """<html><div class="teamsBox"><div class="teamName">Vitality</div><div class="teamName">Spirit</div></div>
<div class="preformatted-text">Best of 3 (LAN)</div><div class="timeAndEvent"><div class="event"><a>IEM Beijing 2026</a></div></div>
<div class="mapholder"><div class="mapname">Mirage</div><div class="results-left won"><div class="results-team-score">13</div></div>
<div class="results-right"><div class="results-team-score">9</div></div></div>
<div class="mapholder"><div class="mapname">Inferno</div><div class="results-left"><div class="results-team-score">8</div></div>
<div class="results-right"><div class="results-team-score">6</div></div></div>
<div class="mapholder"><div class="mapname">TBA</div><div class="results-team-score">-</div><div class="results-team-score">-</div></div>
<div class="countdown">LIVE</div></html>"""


def test_hltv_synthetic():
    ms = parse_match_list(HLTV_LIST)
    assert ms[0].status == "live" and ms[0].event == "IEM Beijing 2026" and ms[0].best_of == 3
    assert ms[1].start_time == 1793600000
    m = parse_match_page(HLTV_PAGE, ms[0])
    assert (m.score1, m.score2, m.current_map, m.status) == (1, 0, "Inferno", "live")
    assert m.live_map.s1 == 8 and m.has_live_score
    res = parse_results('<div class="result-con" data-zonedgrouping-entry-unix="1793500000000"><a href="/matches/9/x">'
                        '<div class="team">A</div><div class="team">B</div><div class="result-score"><span>16</span>'
                        '<span>14</span></div><div class="map-text">mrg</div><div class="event-name">E</div></a></div>')
    assert (res[0].score1, res[0].score2, res[0].maps[0].s1) == (1, 0, 16)
